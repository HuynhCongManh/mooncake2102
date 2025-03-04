# -*- coding: utf-8 -*-
"""Copy of TFF.ipynb

Automatically generated by Colaboratory.

Original file is located at
    https://colab.research.google.com/drive/1b2Vs_wPhVWZRuFQPtzBr-KQZYVQAJwU9
"""

# !pip install --quiet --upgrade tensorflow-federated-nightly
# !pip install --quiet --upgrade tensorflow-model-optimization
# !pip install --quiet --upgrade nest-asyncio

import nest_asyncio
nest_asyncio.apply()

# !pip install tensorflow_text

import tensorflow_text as tf_text

# Commented out IPython magic to ensure Python compatibility.
# %load_ext tensorboard

import functools

import numpy as np
import tensorflow as tf
import tensorflow_federated as tff

from tensorflow_model_optimization.python.core.internal import tensor_encoding as te

import collections
import io
import os
import pandas as pd
import requests
import tempfile
import zipfile

# Load simulation data.
source, _ = tff.simulation.datasets.emnist.load_data()
def client_data(n: int) -> tf.data.Dataset:
  return source.create_tf_dataset_for_client(source.client_ids[n]).map(
      lambda e: (tf.reshape(e['pixels'], [-1]), e['label'])
  ).repeat(10).batch(20)

# Pick a subset of client devices to participate in training.
train_data = [client_data(n) for n in range(3)]

# Wrap a Keras model for use with TFF.
def model_fn() -> tff.learning.Model:
  model = tf.keras.models.Sequential([
      tf.keras.layers.Dense(10, tf.nn.softmax, input_shape=(784,),
                            kernel_initializer='zeros')
  ])
  return tff.learning.from_keras_model(
      model,
      input_spec=train_data[0].element_spec,
      loss=tf.keras.losses.SparseCategoricalCrossentropy(),
      metrics=[tf.keras.metrics.SparseCategoricalAccuracy()])

# Simulate a few rounds of training with the selected client devices.
trainer = tff.learning.build_federated_averaging_process(
  model_fn,
  client_optimizer_fn=lambda: tf.keras.optimizers.SGD(0.1))
state = trainer.initialize()
for _ in range(5):
  state, metrics = trainer.next(state, train_data)
  print(metrics['train']['loss'])



# Below we define several functions that we'll use later, but their details
# aren't as important as their usage in the next cell.
def download_movielens_data(dataset_path):
  r = requests.get(dataset_path)
  z = zipfile.ZipFile(io.BytesIO(r.content))
  z.extractall(path='/tmp')

def load_movielens_data(data_directory="/tmp"):
  """Loads MovieLens ratings from data directory."""
  ratings_df = pd.read_csv(
      os.path.join(data_directory, "ml-1m", "ratings.dat"),
      sep="::",
      names=["UserID", "MovieID", "Rating", "Timestamp"], engine="python")
  # Map movie and user IDs to [0, vocab_size).
  movie_mapping = {
      old_movie: new_movie for new_movie, old_movie in enumerate(
          ratings_df.MovieID.astype("category").cat.categories)
  }
  user_mapping = {
      old_user: new_user for new_user, old_user in enumerate(
          ratings_df.UserID.astype("category").cat.categories)
  }
  ratings_df.MovieID = ratings_df.MovieID.map(movie_mapping)
  ratings_df.UserID = ratings_df.UserID.map(user_mapping)
  return ratings_df

def create_tf_datasets(ratings_df,
                       batch_size=5,
                       max_examples_per_user=300,
                       max_clients=2000,
                       train_fraction=0.8):
  """Creates train and test TF Datasets containing the ratings for all users."""
  num_users = len(set(ratings_df.UserID))
  # Limit to `max_clients` to speed up data loading.
  num_users = min(num_users, max_clients)

  def rating_batch_map_fn(rating_batch):
    return collections.OrderedDict([
        ("x", tf.cast(rating_batch[:, 1:2], tf.int64)),
        ("y", tf.cast(rating_batch[:, 2:3], tf.float32))
    ])

  tf_datasets = []
  for user_id in range(num_users):
    user_ratings_df = ratings_df[ratings_df.UserID == user_id]
    tf_dataset = tf.data.Dataset.from_tensor_slices(user_ratings_df)

    # Define preprocessing operations.
    tf_dataset = tf_dataset.take(max_examples_per_user).shuffle(
        buffer_size=max_examples_per_user, seed=42).batch(batch_size).map(
        rating_batch_map_fn,
        num_parallel_calls=tf.data.experimental.AUTOTUNE)
    tf_datasets.append(tf_dataset)

  np.random.seed(42)
  np.random.shuffle(tf_datasets)
  train_idx = int(len(tf_datasets) * train_fraction)
  return (tf_datasets[:train_idx], tf_datasets[train_idx:])
  

class UserEmbedding(tf.keras.layers.Layer):
  """Keras layer representing an embedding for a single user, used below."""

  def __init__(self, num_latent_factors, **kwargs):
    super().__init__(**kwargs)
    self.num_latent_factors = num_latent_factors

  def build(self, input_shape):
    self.embedding = self.add_weight(
        shape=(1, self.num_latent_factors),
        initializer='uniform',
        dtype=tf.float32,
        name='UserEmbeddingKernel')
    super().build(input_shape)

  def call(self, inputs):
    return self.embedding

  def compute_output_shape(self):
    return (1, self.num_latent_factors)

def get_matrix_factorization_model(
    num_items: int = 3706,
    num_latent_factors: int = 50) -> tff.learning.reconstruction.Model:
  """Defines a Keras matrix factorization model."""
  # Layers with variables will be partitioned into global and local layers.
  # We'll pass this to `tff.learning.reconstruction.from_keras_model`.
  global_layers = []
  local_layers = []

  item_input = tf.keras.layers.Input(shape=[1], name='Item')
  item_embedding_layer = tf.keras.layers.Embedding(
      num_items,
      num_latent_factors,
      name='ItemEmbedding')
  global_layers.append(item_embedding_layer)
  flat_item_vec = tf.keras.layers.Flatten(name='FlattenItems')(
      item_embedding_layer(item_input))

  user_embedding_layer = UserEmbedding(
      num_latent_factors,
      name='UserEmbedding')
  local_layers.append(user_embedding_layer)

  # The item_input never gets used by the user embedding layer,
  # but this allows the model to directly use the user embedding.
  flat_user_vec = user_embedding_layer(item_input)

  pred = tf.keras.layers.Dot(
      1, normalize=False, name='Dot')([flat_user_vec, flat_item_vec])

  input_spec = collections.OrderedDict(
      x=tf.TensorSpec(shape=[None, 1], dtype=tf.int64),
      y=tf.TensorSpec(shape=[None, 1], dtype=tf.float32))

  model = tf.keras.Model(inputs=item_input, outputs=pred)
  return tff.learning.reconstruction.from_keras_model(
      keras_model=model,
      global_layers=global_layers,
      local_layers=local_layers,
      input_spec=input_spec)
  
class RatingAccuracy(tf.keras.metrics.Mean):
  """Keras metric computing accuracy of reconstructed ratings."""

  def __init__(self, name='rating_accuracy', **kwargs):
    super().__init__(name=name, **kwargs)

  def update_state(self, y_true, y_pred, sample_weight=None):
    absolute_diffs = tf.abs(y_true - y_pred)
    example_accuracies = tf.less_equal(absolute_diffs, 0.5)
    super().update_state(example_accuracies, sample_weight=sample_weight)

download_movielens_data('http://files.grouplens.org/datasets/movielens/ml-1m.zip')
ratings_df = load_movielens_data()
print(ratings_df.head())
tf_train_datasets, tf_test_datasets = create_tf_datasets(ratings_df)

model_fn = get_matrix_factorization_model
loss_fn = lambda: tf.keras.losses.MeanSquaredError()
metrics_fn = lambda: [RatingAccuracy()]

training_process = tff.learning.reconstruction.build_training_process(
    model_fn=model_fn,
    loss_fn=loss_fn,
    metrics_fn=metrics_fn,
    server_optimizer_fn=lambda: tf.keras.optimizers.SGD(1.0),
    client_optimizer_fn=lambda: tf.keras.optimizers.SGD(0.5),
    reconstruction_optimizer_fn=lambda: tf.keras.optimizers.SGD(0.1))

evaluation_computation = tff.learning.reconstruction.build_federated_evaluation(
    model_fn,
    loss_fn=loss_fn,
    metrics_fn=metrics_fn,
    reconstruction_optimizer_fn=lambda: tf.keras.optimizers.SGD(0.1))

state = training_process.initialize()
for i in range(10):
  federated_train_data = np.random.choice(tf_train_datasets, size=50, replace=False).tolist()
  state, metrics = training_process.next(state, federated_train_data)
  print(f'Train round {i}:', metrics['train'])

eval_metrics = evaluation_computation(state.model, tf_test_datasets)
print('Final Eval:', eval_metrics['eval'])

# Load simulation data.
source, _ = tff.simulation.datasets.emnist.load_data()
def client_data(n: int) -> tf.data.Dataset:
  return source.create_tf_dataset_for_client(source.client_ids[n]).map(
      lambda e: (tf.reshape(e['pixels'], [-1]), e['label'])
  ).repeat(10).batch(20)

# Pick a subset of client devices to participate in training.
train_data = [client_data(n) for n in range(3)]

# Wrap a Keras model for use with TFF.
def model_fn() -> tff.learning.Model:
  model = tf.keras.models.Sequential([
      tf.keras.layers.Dense(10, tf.nn.softmax, input_shape=(784,),
                            kernel_initializer='zeros')
  ])
  return tff.learning.from_keras_model(
      model,
      input_spec=train_data[0].element_spec,
      loss=tf.keras.losses.SparseCategoricalCrossentropy(),
      metrics=[tf.keras.metrics.SparseCategoricalAccuracy()])
  
# Construct DP model update aggregator.
model_update_aggregator = tff.learning.dp_aggregator(
  noise_multiplier=1e-3,   # z: Determines privacy epsilon.
  clients_per_round=3)     # Aggregator needs number of clients per round.

# Build FedAvg process with custom aggregator.
trainer = tff.learning.build_federated_averaging_process(
  model_fn,
  client_optimizer_fn=lambda: tf.keras.optimizers.SGD(0.1),
  model_update_aggregation_factory=model_update_aggregator)

# Simulate a few rounds of training with the selected client devices.
state = trainer.initialize()
for _ in range(5):
  state, metrics = trainer.next(state, train_data)
  print(metrics['train']['loss'])

# Load simulation data.
source, _ = tff.simulation.datasets.emnist.load_data()
def client_data(n: int) -> tf.data.Dataset:
  return source.create_tf_dataset_for_client(source.client_ids[n]).map(
      lambda e: (tf.reshape(e['pixels'], [-1]), e['label'])
  ).repeat(10).batch(20)

# Pick a subset of client devices to participate in training.
train_data = [client_data(n) for n in range(3)]

# Wrap a Keras model for use with TFF.
def model_fn() -> tff.learning.Model:
  model = tf.keras.models.Sequential([
      tf.keras.layers.Dense(10, tf.nn.softmax, input_shape=(784,),
                            kernel_initializer='zeros')
  ])
  return tff.learning.from_keras_model(
      model,
      input_spec=train_data[0].element_spec,
      loss=tf.keras.losses.SparseCategoricalCrossentropy(),
      metrics=[tf.keras.metrics.SparseCategoricalAccuracy()])
  
# Construct Tree Aggregation aggregator for DP-FTRL.
model_weight_specs = tff.framework.type_to_tf_tensor_specs(
        tff.learning.framework.weights_type_from_model(model_fn).trainable)
model_update_aggregator = tff.aggregators.DifferentiallyPrivateFactory.tree_aggregation(
    noise_multiplier=1e-3,
    clients_per_round=3,
    l2_norm_clip=1.,
    record_specs=model_weight_specs)  

# Build FedAvg process with custom aggregator.
trainer = tff.learning.build_federated_averaging_process(
  model_fn,
  client_optimizer_fn=lambda: tf.keras.optimizers.SGD(0.1),
  model_update_aggregation_factory=model_update_aggregator)

# Simulate a few rounds of training with the selected client devices.
state = trainer.initialize()
for _ in range(5):
  state, metrics = trainer.next(state, train_data)
  print(metrics['train']['loss'])

model_fn.save('model')

# Load the simulation data.
source, _ = tff.simulation.datasets.shakespeare.load_data()

# Preprocessing funtion to tokenize a line into words.
@tf.function
def tokenize(ds):
  """Tokenizes a line into words with alphanum characters."""
  def extract_strings(example):
    return tf.expand_dims(example['snippets'], 0)

  def tokenize_line(line):
    return tf.data.Dataset.from_tensor_slices(tokenizer.tokenize(line)[0])

  def mask_all_symbolic_words(word):
    return tf.math.logical_not(
        tf_text.wordshape(word, tf_text.WordShape.IS_PUNCT_OR_SYMBOL))

  tokenizer = tf_text.WhitespaceTokenizer()
  ds = ds.map(extract_strings)
  ds = ds.flat_map(tokenize_line)
  ds = ds.map(tf_text.case_fold_utf8)
  ds = ds.filter(mask_all_symbolic_words)
  return ds

# Arguments for the PHH computation
batch_size = 5
max_words_per_user = 8

def client_data(n: int) -> tf.data.Dataset:
  return tokenize(source.create_tf_dataset_for_client(
      source.client_ids[n])).batch(batch_size)

# Pick a subset of client devices to participate in the PHH computation.
dataset = [client_data(n) for n in range(10)]

def run_simulation(one_round_computation: tff.Computation, dataset):
  output = one_round_computation(dataset)
  heavy_hitters = output.heavy_hitters
  heavy_hitters_counts = output.heavy_hitters_counts
  heavy_hitters = [word.decode('utf-8', 'ignore') for word in heavy_hitters]

  results = {}
  for index in range(len(heavy_hitters)):
    results[heavy_hitters[index]] = heavy_hitters_counts[index]
  return dict(results)

iblt_computation = tff.analytics.heavy_hitters.iblt.build_iblt_computation(
    capacity=100,
    max_string_length=20,
    max_words_per_user=max_words_per_user,
    max_heavy_hitters=10,
    multi_contribution=False,
    batch_size=batch_size)

result = run_simulation(iblt_computation, dataset)
print(f'result without DP: {result}')

# DP parameters
eps = 20
delta = 0.01

# Calculating scale for Laplace noise
scale = max_words_per_user / eps

# Calculating the threshold
tau = 1 + (max_words_per_user / eps) * np.log(max_words_per_user / (2 * delta))

result_with_dp = {}
for word in result:
  noised_count = result[word] + np.random.laplace(scale=scale)
  if noised_count >= tau:
    result_with_dp[word] = noised_count
print(f'result with DP: {result_with_dp}')