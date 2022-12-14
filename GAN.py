from base64 import encode
from cgitb import text
from hashlib import new
from msilib import sequence
import tensorflow as tf
from keras.models import Model
from keras.optimizers import Adam, RMSprop, SGD
from keras.layers import Dense, Concatenate, LSTM, Embedding, GRU, InputLayer, Flatten, Reshape
from tensorflow_text import BertTokenizer, WordpieceTokenizer
from keras.utils import pad_sequences
from tensorflow.lookup import StaticVocabularyTable, KeyValueTensorInitializer
import numpy as np
import os
import time
from multiprocessing import pool
import pandas as pd

from Encoder import Encoder
from Generator import Generator
from Discriminator import Discriminator
from AutoEncoder import AutoEncoder




class GAN(Model):
    def __init__(self, text, maxlen, batch_size = 32) -> None:
        super(GAN, self).__init__()
        self.cross_entropy = tf.keras.losses.BinaryCrossentropy(from_logits=True)
        self.encoder = Encoder(text, maxlen)
        self.generator = Generator(self.encoder.vocab_len, self.encoder.sequence_maxlen)
        self.discriminator = Discriminator()
        self.ae = AutoEncoder(self.encoder.vocab_len, self.encoder.sequence_maxlen)

        self.gen_opt = Adam(3e-5)
        self.discrim_opt = Adam(2e-6)

        self.generator.compile(optimizer=self.gen_opt)
        self.discriminator.compile(optimizer=self.discrim_opt)
        self.ae.compile(optimizer="adam", loss="categorical_crossentropy", metrics=["accuracy"])

        self.vocab_len = self.encoder.vocab_len
        self.sequence_len = self.encoder.sequence_maxlen
        self.ckpt, self.ckpt_prefix = self.get_checkpoint()
        self.generator_gradients = []
        self.discriminator_gradients = []
        self.batch_size = batch_size

        print("Vocab length", self.encoder.vocab_len)

    def generate_noise(self):
        return np.random.normal(0, 0.3, 5000) * self.vocab_len

    @tf.function
    def minmax_loss(self, real_output, fake_output):
        real_loss = self.cross_entropy(tf.ones_like(real_output), real_output)
        fake_loss = self.cross_entropy(tf.zeros_like(fake_output), fake_output)
        discriminator_loss = real_loss + fake_loss

        generator_loss = self.cross_entropy(tf.ones_like(fake_output), fake_output)

        return generator_loss*100, discriminator_loss*100
    
    def wasserstein_loss(self, real_output, fake_ouptut):
        return -fake_ouptut*100, (fake_ouptut-real_output)*100

    def modefied_minmax_loss(self, real_output, fake_output):
        real_loss = tf.math.log(real_output[0][0])
        fake_loss = tf.math.log((1-fake_output[0][0]))
        discriminator_loss = real_loss + fake_loss

        generator_loss = -tf.math.log(fake_output)

        return generator_loss, discriminator_loss
    
    def least_squared_loss(self, real_output, fake_output):
        pass
    
    def get_checkpoint(self):
        checkpoint_dir = './training_checkpoints'
        checkpoint_prefix = os.path.join(checkpoint_dir, "ckpt")
        checkpoint = tf.train.Checkpoint(
            generator=self.generator,
            discriminator=self.discriminator,
            generator_optimizer=self.gen_opt,
            discriminator_optimizer=self.discrim_opt
        )
        return checkpoint, checkpoint_prefix

    def save_encoded_data(self, text_data):
        formatted_data = []
        for datapoint in text_data:
            x = self.encoder.encode(datapoint)
            formatted_data.append(x)
        data = pd.DataFrame({"data": formatted_data})
        data.to_pickle("FormattedData.pkl")
        
    def train_autoencoder(self, text_data, epochs=100, verbose=1, batch_size=128):
        print("Training Autoencoder")
        formatted_data = [self.encoder.encode(datapoint) for datapoint in text_data]
        formatted_data = np.reshape(formatted_data, (len(text_data),self.sequence_len,self.vocab_len))
        for epoch in range(epochs):
            self.ae.fit(formatted_data, formatted_data, epochs=1, verbose=verbose, batch_size=batch_size)
            if epoch%10 ==0 and epoch>10:
                idx = np.random.randint(low=0, high=len(formatted_data)-1)
                dp = formatted_data[idx]
                dp = np.reshape(dp, (1,5,8686))
                sent = self.ae.predict(dp, verbose=0)
                print("real text:", self.encoder.decode(formatted_data[idx]), "AE text:", self.encoder.decode(sent))
                self.ae.save_weights("AutoEncoderWeights/aeWeights")
            
    
    def test_autoencoder(self, text_data, verbose=1):
        formatted_data = []
        for datapoint in text_data:
            x = self.encoder.encode(datapoint)
            formatted_data.append(x)
        formatted_data = np.reshape(formatted_data, (len(text_data),self.sequence_len,self.vocab_len))
        self.ae.evaluate(formatted_data, formatted_data, verbose)

    def create_text_representation(self, generator_text, timesteps):
        new_generator_text = []
        marker = 0
        for _ in range(self.sequence_len):
            if marker >= timesteps:
                marker = 0
            new_generator_text.append(generator_text[marker])
            marker += 1
        return new_generator_text

    @tf.function(jit_compile=True)    
    def trainstep(self, real_texts, noises, batch_size, gen_updates=1, discrim_updates=0):
        generator_gradients = []
        discriminator_gradients = []
        generated_texts = []
        for real_text, noise in zip(real_texts, noises):
            with tf.GradientTape() as tape1, tf.GradientTape() as tape2:
                
                fake_text = self.generator(noise, training=True)
                fake_text = tf.cast(fake_text, dtype=tf.float32)
                #fake_text *= self.vocab_len
                generated_texts.append(fake_text)
                
                fake_text = tf.reshape(fake_text, (1,self.encoder.sequence_maxlen, 1))
                #fake_text += 0.05 * tf.random.uniform(tf.shape(fake_text))
                real_text = real_text[0]
                real_text = tf.reshape(real_text, (1,self.encoder.sequence_maxlen, 1))

                #real_text = 0.05 * tf.random.uniform(tf.shape(real_text))
                fake_text_y = self.discriminator(fake_text, training=True)
                real_text_y = self.discriminator(real_text, training=True)

                generator_loss, discriminator_loss = self.wasserstein_loss(real_text_y, fake_text_y)

            generator_gradients.append(tape1.gradient(generator_loss, self.generator.trainable_variables))
            discriminator_gradients.append(tape2.gradient(discriminator_loss, self.discriminator.trainable_variables))

        generator_gradient = generator_gradients[0]
        discriminator_gradient = discriminator_gradients[0]
        for gradient_idx in range(len(generator_gradients)-1):
            generator_gradient += generator_gradients[gradient_idx+1]
            discriminator_gradient += discriminator_gradients[gradient_idx+1]
            


        generator_gradient = [gen_grad/batch_size for gen_grad in generator_gradient]
        discriminator_gradient = [dis_grad/batch_size for dis_grad in discriminator_gradient]

        for _ in range(gen_updates):
            self.gen_opt.apply_gradients(zip(generator_gradient, self.generator.trainable_variables))
        
        for _ in range(discrim_updates):
            self.discrim_opt.apply_gradients(zip(discriminator_gradient, self.discriminator.trainable_variables))
        
        return generator_loss, discriminator_loss, generated_texts, real_text, fake_text_y, real_text_y
       
    def format_tokens(self, tokens):
        new_tokens = np.zeros((self.sequence_len, self.sequence_len, self.vocab_len))
        for timestep in range(new_tokens.shape[0]):
            for idx in range(len(new_tokens)):
                if idx > timestep:
                    break
                new_tokens[timestep][idx] = tokens[0][idx]

        return new_tokens
                
        

    def train(self, text_data, noises, epochs, ckpt_freq=500, print_freq=1, batch_size=32):
        for epoch in range(1, epochs+1):
            marker = 0
            datalen = np.array(noises).shape[0]-1
            while True:
                start_time = time.time()
                batch_data_num = 0
                token_batch = []
                noise_batch = []
                if marker >= datalen:
                    break

                while True:
                    if batch_data_num >= batch_size:
                        break
                    elif marker >= datalen:
                        break

                    noise = np.expand_dims(noises[marker], axis=0)
                    noise = np.expand_dims(noise, axis=0)
                    tokens = self.encoder.encode(text_data[marker])

                    if len([token[0] for token in tokens[0]]) > self.encoder.sequence_maxlen:
                        continue
                    

                    token_batch.append(tokens)
                    noise_batch.append(noise)



                    batch_data_num += 1
                    marker += 1

                g_loss, d_loss, generated_text, real_text, fy, ry = self.trainstep(token_batch, noise_batch, batch_size)
                generated_text = tf.reshape(generated_text[0], (1, 1, self.sequence_len))
                generated_text = self.encoder.decode(generated_text)
                real_text = tf.reshape(real_text, (1, 1, self.sequence_len))
                real_text = self.encoder.decode(real_text)

                if int(marker/batch_size) % print_freq == 0:
                    print(f"""Epoch: {epoch} | Batch: {int(marker/batch_size)} | Time: {time.time()-start_time}\n
                        Generator loss: {g_loss} | Discriminator loss: {d_loss}\n
                        Generator text: {generated_text}\n
                        Real text: {real_text}\n
                        Discriminator Predictions - Generator: {fy} | Discriminator: {ry}"""
                    )

                if int(marker/batch_size) % ckpt_freq == 0:
                    self.ckpt.save(file_prefix=self.ckpt_prefix)
    
    def generate(self, x):
        sentence = np.zeros((1,self.generator.sequence_len, self.generator.vocab_len))
        for idx in range(self.generator.sequence_len):
            next_word = self.generator(x, sentence)
            sentence[0][idx] = next_word[0]

        return self.encoder.decode(sentence)