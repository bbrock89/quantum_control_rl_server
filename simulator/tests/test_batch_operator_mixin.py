import unittest

import tensorflow as tf

from simulator.operators import momentum, position
from simulator.mixins import BatchOperatorMixin
from .utils import coeff_err, random_alphas


class TestHarnessClass(BatchOperatorMixin):
    """Wrapper harness for mixin"""
    def __init__(self):
        self.p = momentum(100)
        self.q = position(100)
        super().__init__()

class TestBatchOperatorMixin(unittest.TestCase):
    def setUp(self):
        self.sim = TestHarnessClass()
        self.alphas = random_alphas(100, maxval=1)

    def test_displace(self):
        mean_err, _ = coeff_err(self.sim.displace(self.alphas), self.alphas)
        self.assertLess(mean_err, 9e-7)

    def test_translate(self):
        scaled_alphas = self.alphas * tf.sqrt(tf.constant(2, dtype=tf.complex64))
        mean_err, _ = coeff_err(self.sim.translate(scaled_alphas), self.alphas)
        self.assertLess(mean_err, 9e-7)
