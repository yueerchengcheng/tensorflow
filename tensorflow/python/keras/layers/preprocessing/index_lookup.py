# Copyright 2020 The TensorFlow Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
"""Keras text vectorization preprocessing layer."""
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import collections
import json
import operator

import numpy as np

from tensorflow.python.framework import dtypes
from tensorflow.python.framework import tensor_shape
from tensorflow.python.framework import tensor_spec
from tensorflow.python.keras.engine import base_preprocessing_layer
from tensorflow.python.keras.layers.preprocessing import table_utils
from tensorflow.python.ops import lookup_ops
from tensorflow.python.util import compat

# The string tokens in the extracted vocabulary
_VOCAB_NAME = "vocab"

# The string tokens in the full vocabulary
_ACCUMULATOR_VOCAB_NAME = "vocab"
# The total counts of each token in the vocabulary
_ACCUMULATOR_COUNTS_NAME = "counts"


class IndexLookup(base_preprocessing_layer.CombinerPreprocessingLayer):
  """Maps strings (or integers) from a vocabulary to integer indices.

  This layer translates a set of arbitrary strings or integers into an integer
  output via a table-based lookup, with optional out-of-vocabulary handling.

  If desired, the user can call this layer's `adapt()` method on a data set,
  which will analyze the data set, determine the frequency of individual string
  or integer values, and create a vocabulary from them. This vocabulary can have
  unlimited size or be capped, depending on the configuration options for this
  layer; if there are more unique values in the input than the maximum
  vocabulary size, the most frequent terms will be used to create the
  vocabulary.

  Attributes:
    max_tokens: The maximum size of the vocabulary for this layer. If None,
      there is no cap on the size of the vocabulary. Note that the vocabulary
      does include OOV buckets, so the effective number of unique values in the
      vocabulary is `(max_tokens - num_oov_tokens)` when this value is set.
    num_oov_tokens: The number of out-of-vocabulary tokens to use; defaults to
      1. If this value is more than 1, OOV inputs are hashed to determine their
      OOV value; if this value is 0, passing an OOV input will result in a '-1'
      being returned for that value in the output tensor. (Note that, because
      the value is -1 and not 0, this will allow you to effectively drop OOV
      values from categorical encodings.)
    vocabulary: An optional list of vocabulary terms, or a path to a text file
      containing a vocabulary to load into this layer. The file should contain
      one token per line. In either case, the vocabulary must be unique; if
      the list or file contains the same token multiple times, an error will
      be thrown. Note that when passing a vocabulary - either as a list or as
      a file - the vocabulary will not be present in the layer's config dict;
      it will instead be a part of the layer's weights.
    reserve_zero: Whether to reserve the index 0, which indicates pad values in
      the Keras masking system. If True, the output of this layer will be in the
      range `[1...max_tokens+1)`; if False, the output will be in the range
      `[0...max_tokens)`. Defaults to True.
    mask_zero: If True, input values of 0 (for integers) and `""` (for strings)
      will be treated as masked values and assigned an output value of 0. If
      this option is set, `reserve_zero` must also be set. Defaults to False.
  Call arguments:
    inputs: The data to look up. Can be a tf.Tensor or RaggedTensor.
    invert: Controls the lookup direction. If False, the layer will map strings
      to integers; if true, the layer will map integers to strings. Defaults
      to False.
  """
  # TODO(momernick): Add an examples section to the docstring.

  def __init__(self,
               max_tokens=None,
               num_oov_tokens=1,
               vocabulary=None,
               reserve_zero=True,
               mask_zero=False,
               **kwargs):
    invert = False
    if invert:
      allowed_dtypes = [dtypes.int32, dtypes.int64]
    else:
      allowed_dtypes = [dtypes.string, dtypes.int32, dtypes.int64]

    if "dtype" in kwargs and kwargs["dtype"] not in allowed_dtypes:
      raise ValueError("TextVectorization may only have a dtype in %s." %
                       allowed_dtypes)

    if "dtype" not in kwargs:
      kwargs["dtype"] = dtypes.int64 if invert else dtypes.string

    # If max_tokens is set, the value must be greater than 1 - otherwise we
    # are creating a 0-element vocab, which doesn't make sense.
    if max_tokens is not None and max_tokens <= 1:
      raise ValueError("If set, max_tokens must be greater than 1.")

    if num_oov_tokens < 0:
      raise ValueError("num_oov_tokens must be greater than 0. You passed %s" %
                       num_oov_tokens)

    self.invert = invert
    self.max_tokens = max_tokens
    self.num_oov_tokens = num_oov_tokens
    self.reserve_zero = reserve_zero
    self.mask_zero = mask_zero

    # We need to reserve at least num_oov_tokens tokens, plus one additional
    # value if we are reserving the zero value in our output.
    if reserve_zero:
      self._reserved_values = (num_oov_tokens + 1)
    else:
      self._reserved_values = num_oov_tokens

    # We need to account for the OOV buckets in our vocabulary size.
    if max_tokens is not None:
      self._max_elements = max_tokens - num_oov_tokens
    else:
      self._max_elements = None

    # If there is only one OOV bucket, we can determine the OOV value (either 0
    # or 1 depending on whether 0 is reserved) and set that as the default
    # value of the index_lookup table. If we hav multiple OOV values, we need to
    # do a further hashing step; to make this easier, we set the OOV value to
    # -1. (This lets us do a vectorized add and cast to boolean to determine
    # locations where we need to do extra hashing.)
    if self.num_oov_tokens == 1:
      self._oov_value = 1 if reserve_zero else 0
    else:
      self._oov_value = -1

    super(IndexLookup, self).__init__(
        combiner=_IndexLookupCombiner(self.max_tokens), **kwargs)

    # If the layer's input type is int32, we can only output int32 values -
    # MutableHashTable doesn't allow us to map int32->int64.
    if self.dtype == dtypes.int32:
      self._output_dtype = dtypes.int32
    else:
      self._output_dtype = dtypes.int64
    self._table = lookup_ops.MutableHashTable(
        key_dtype=self.dtype,
        value_dtype=self._output_dtype,
        default_value=self._oov_value,
        name=(self._name + "_index_table"))
    tracked_table = self._add_trackable(self._table, trainable=False)
    # This is a workaround for summary() on this layer. Because the table is
    # not mutable during training, the effective number of parameters (and so
    # the weight shape) is 0; we add this as an attr so that the parameter
    # counting code in the Model object doesn't throw an attribute error.
    tracked_table.shape = tensor_shape.TensorShape((0,))

    if self.num_oov_tokens <= 1:
      oov_tokens = None
    else:
      oov_start = 1 if reserve_zero else 0
      oov_tokens = list(range(oov_start, self._reserved_values))

    self._table_handler = table_utils.TableHandler(
        table=self._table,
        oov_tokens=oov_tokens,
        use_v1_apis=self._use_v1_apis())

    if vocabulary is not None:
      if isinstance(vocabulary, str):
        vocabulary = table_utils.get_vocabulary_from_file(vocabulary)
      table_utils.validate_vocabulary_is_unique(vocabulary)

      self.set_vocabulary(vocabulary)

  def compute_output_shape(self, input_shape):
    return input_shape

  def compute_output_signature(self, input_spec, invert=False):
    output_shape = self.compute_output_shape(input_spec.shape.as_list())
    if invert:
      output_dtype = dtypes.string
    else:
      output_dtype = dtypes.int64
    return tensor_spec.TensorSpec(shape=output_shape, dtype=output_dtype)

  def adapt(self, data, reset_state=True):
    """Fits the state of the preprocessing layer to the dataset.

    Overrides the default adapt method to apply relevant preprocessing to the
    inputs before passing to the combiner.

    Arguments:
      data: The data to train on. It can be passed either as a tf.data Dataset,
        or as a numpy array.
      reset_state: Optional argument specifying whether to clear the state of
        the layer at the start of the call to `adapt`. This must be True for
        this layer, which does not support repeated calls to `adapt`.
    """
    if not reset_state:
      raise ValueError("IndexLookup does not support streaming adapts.")
    super(IndexLookup, self).adapt(data, reset_state)

  def get_vocabulary(self):
    if self._table_handler.vocab_size() == 0:
      return []

    keys, values = self._table_handler.data()
    # This is required because the MutableHashTable doesn't preserve insertion
    # order, but we rely on the order of the array to assign indices.
    if self.dtype == dtypes.string:
      return [x.decode("utf-8") for _, x in sorted(zip(values, keys))]
    else:
      return [x for _, x in sorted(zip(values, keys))]

  def vocab_size(self):
    return self._table_handler.vocab_size()

  def get_config(self):
    config = {
        "max_tokens": self.max_tokens,
        "num_oov_tokens": self.num_oov_tokens,
        "vocabulary": None,
        "reserve_zero": self.reserve_zero,
        "mask_zero": self.mask_zero,
    }
    base_config = super(IndexLookup, self).get_config()
    return dict(list(base_config.items()) + list(config.items()))

  def count_params(self):
    # This method counts the number of scalars in the weights of this layer.
    # Since this layer doesn't have any /actual/ weights (in that there's
    # nothing in this layer that can be trained - we only use the weight
    # abstraction for ease of saving!) we return 0.
    return 0

  def set_vocabulary(self,
                     vocab,
                     append=False):
    """Sets vocabulary (and optionally document frequency) data for this layer.

    This method sets the vocabulary for this layer directly, instead of
    analyzing a dataset through 'adapt'. It should be used whenever the vocab
    information is already known. If vocabulary data is already present in the
    layer, this method will either replace it, if 'append' is set to False, or
    append to it (if 'append' is set to True).

    Arguments:
      vocab: An array of string tokens.
      append: Whether to overwrite or append any existing vocabulary data.

    Raises:
      ValueError: If there are too many inputs, the inputs do not match, or
        input data is missing.
    """
    current_table_size = self._table_handler.vocab_size()
    total_vocab_size = len(vocab) + (current_table_size if append else 0)
    if self.max_tokens is not None and total_vocab_size > self._max_elements:
      raise ValueError(
          "Attempted to set a vocabulary larger than the maximum vocab size. "
          "Passed vocab size is %s, max vocab size is %s. Note that the OOV "
          "token(s) are automatically added to the number of tokens." %
          (total_vocab_size, self.max_tokens))

    start_index = self._reserved_values + (current_table_size if append else 0)
    values = np.arange(start_index, len(vocab) + start_index, dtype=np.int64)
    vocab = table_utils.convert_to_ndarray(vocab, self.dtype)
    table_utils.assert_same_type(self.dtype, vocab, "vocab")

    values = table_utils.convert_to_ndarray(values, self._output_dtype)
    table_utils.assert_same_type(self._output_dtype, values, "values")

    if not append and current_table_size > 0:
      self._table_handler.clear()
    self._table_handler.insert(vocab, values)

  def _set_state_variables(self, updates):
    if not self.built:
      raise RuntimeError("_set_state_variables() must be called after build().")
    self.set_vocabulary(updates[_VOCAB_NAME])

  def call(self, inputs):
    return self._table_handler.lookup(inputs)

  def _use_v1_apis(self):
    return False


class _IndexLookupAccumulator(
    collections.namedtuple("Accumulator", ["count_dict"])):
  pass


class _IndexLookupCombiner(base_preprocessing_layer.Combiner):
  """Combiner for the IndexLookup preprocessing layer.

  This class encapsulates the logic for computing a vocabulary based on the
  frequency of each token.

  Attributes:
    vocab_size: (Optional) If set, only the top `vocab_size` tokens (based on
      frequency across the dataset) are retained in the vocabulary. If None, or
      set to a value greater than the total number of distinct tokens in the
      dataset, all tokens are retained.s
  """

  def __init__(self, vocab_size=None):
    self._vocab_size = vocab_size

  def compute(self, values, accumulator=None):
    """Compute a step in this computation, returning a new accumulator."""
    values = base_preprocessing_layer.convert_to_list(values)

    if accumulator is None:
      accumulator = self._create_accumulator()

    # TODO(momernick): Benchmark improvements to this algorithm.
    if isinstance(values, (str, bytes)):
      accumulator.count_dict[values] += 1
    else:
      for document in values:
        if not isinstance(document, list):
          accumulator.count_dict[document] += 1
        else:
          for token in document:
            accumulator.count_dict[token] += 1

    return accumulator

  def merge(self, accumulators):
    """Merge several accumulators to a single accumulator."""
    if not accumulators:
      return accumulators

    base_accumulator = accumulators[0]
    for accumulator in accumulators[1:]:
      for token, value in accumulator.count_dict.items():
        base_accumulator.count_dict[token] += value

    return base_accumulator

  def extract(self, accumulator):
    """Convert an accumulator into a dict of output values.

    Args:
      accumulator: An accumulator aggregating over the full dataset.

    Returns:
      A dict of:
        "vocab": A list of the retained items in the vocabulary.
    """
    vocab_counts = accumulator.count_dict
    sorted_counts = sorted(
        vocab_counts.items(), key=operator.itemgetter(1, 0), reverse=True)
    vocab_data = (
        sorted_counts[:self._vocab_size] if self._vocab_size else sorted_counts)
    vocab = [data[0] for data in vocab_data]
    return {_VOCAB_NAME: vocab}

  def restore(self, output):
    """Create an accumulator based on 'output'."""
    raise NotImplementedError(
        "IndexLookup does not restore or support streaming updates.")

  def serialize(self, accumulator):
    """Serialize an accumulator for a remote call."""
    output_dict = {}
    output_dict["vocab"] = list(accumulator.count_dict.keys())
    output_dict["vocab_counts"] = list(accumulator.count_dict.values())
    return compat.as_bytes(json.dumps(output_dict))

  def deserialize(self, encoded_accumulator):
    """Deserialize an accumulator received from 'serialize()'."""
    accumulator_dict = json.loads(compat.as_text(encoded_accumulator))

    accumulator = self._create_accumulator()
    count_dict = dict(
        zip(accumulator_dict["vocab"], accumulator_dict["vocab_counts"]))
    accumulator.count_dict.update(count_dict)

    return accumulator

  def _create_accumulator(self):
    """Accumulate a sorted array of vocab tokens and corresponding counts."""

    count_dict = collections.defaultdict(int)
    return _IndexLookupAccumulator(count_dict)
