# -*- coding: utf-8 -*-
"""
Created on Mon May  4 14:30:01 2020

@author: Vladimir Sivak
"""

import qutip as qt
from numpy import pi, sqrt
import tensorflow as tf
from tensorflow import complex64 as c64
from tensorflow.keras.backend import batch_dot
from gkp.gkp_tf_env.gkp_tf_env import GKP

class OscillatorGKP(GKP):
    """
    This class inherits simulation-independent functionality from the GKP
    class and implements simulation by abstracting away the qubit and using
    Kraus maps formalism to rather efficiently simulate operations on the
    oscillator Hilbert space. 
    
    """
    
    def __init__(self, **kwargs):
        self.tensorstate = False
        super(OscillatorGKP, self).__init__(**kwargs)

    def define_operators(self):
        """
        Define all relevant operators as tensorflow tensors of shape [N,N].
        Methods need to take care of batch dimension explicitly. 
        
        """
        N = self.N
        # Create qutip tensors
        I = qt.identity(N)
        a = qt.destroy(N)
        a_dag = qt.create(N)
        q = (a.dag() + a) / sqrt(2)
        p = 1j*(a.dag() - a) / sqrt(2)
        n = qt.num(N)
        
        Hamiltonian = -1/2*(2*pi)*self.K_osc*n*n  # Kerr
        c_ops = [sqrt(1/self.T1_osc)*a]           # photon loss

        # Convert to tensorflow tensors
        self.I = tf.constant(I.full(), dtype=c64)
        self.a = tf.constant(a.full(), dtype=c64)
        self.a_dag = tf.constant(a_dag.full(), dtype=c64)
        self.q = tf.constant(q.full(), dtype=c64)
        self.p = tf.constant(p.full(), dtype=c64)
        self.n = tf.constant(n.full(), dtype=c64)

        self.Hamiltonian = tf.constant(Hamiltonian.full(), dtype=c64)
        self.c_ops = [tf.constant(op.full(), dtype=c64) for op in c_ops]        
        
        
    @tf.function
    def quantum_circuit_v1(self, psi, action):
        """
        Apply Kraus map version 1. In this version conditional translation by
        'beta' is not symmetric (translates by beta if qubit is in state 1). 
        
        Input:
            action -- dictionary of batched actions. Dictionary keys are
                      'alpha', 'beta', 'phi'
            
        Output:
            psi_final -- batch of final states; shape=[batch_size,N]
            psi_cached -- batch of cached states; shape=[batch_size,N]
            obs -- measurement outcomes; shape=(batch_size,)
            
        """
        # extract parameters
        alpha = self.vec_to_complex(action['alpha'])
        beta = self.vec_to_complex(action['beta'])
        phi = action['phi']
        
        Kraus = {}
        T = {'a' : self.translate(alpha),
             'b' : self.translate(beta)}
        I = tf.stack([self.I]*self.batch_size)
        Kraus[0] = 1/2*(I + self.phase(phi)*T['b'])
        Kraus[1] = 1/2*(I - self.phase(phi)*T['b'])
        
        psi = self.mc_sim_delay.run(psi)
        psi_cached = batch_dot(T['a'], psi)
        psi = self.mc_sim_round.run(psi_cached)
        psi = self.normalize(psi)
        psi_final, obs = self.measurement(psi, Kraus)
        
        return psi_final, psi_cached, obs

    @tf.function
    def quantum_circuit_v2(self, psi, action):
        """
        Apply Kraus map version 2. In this version conditional translation by
        'beta' is symmetric (translates by +-beta/2 controlled by the qubit)
        
        Input:
            action -- batch of actions; shape=[batch_size,5]
            
        Output:
            psi_final -- batch of final states; shape=[batch_size,N]
            psi_cached -- batch of cached states; shape=[batch_size,N]
            obs -- measurement outcomes; shape=(batch_size,)
            
        """
        # extract parameters
        alpha = self.vec_to_complex(action['alpha'])
        beta = self.vec_to_complex(action['beta'])
        phi = action['phi']
        
        Kraus = {}
        T = {'a' : self.translate(alpha),
             'b' : self.translate(beta/2.0)}
        Kraus[0] = 1/2*(tf.linalg.adjoint(T['b']) + self.phase(phi)*T['b'])
        Kraus[1] = 1/2*(tf.linalg.adjoint(T['b']) - self.phase(phi)*T['b'])

        psi = self.mc_sim_delay.run(psi)
        psi_cached = batch_dot(T['a'], psi)
        psi = self.mc_sim_round.run(psi_cached)
        psi = self.normalize(psi)
        psi_final, obs = self.measurement(psi, Kraus)
        
        return psi_final, psi_cached, obs


    @tf.function
    def quantum_circuit_v3(self, psi, action):
        """
        Apply Kraus map version 3. This is a protocol proposed by Baptiste.
        It essentially combines trimming and sharpening in a single round. 
        Trimming is controlled by 'epsilon'.
        
        Input:
            action -- dictionary of batched actions. Dictionary keys are
                      'alpha', 'beta', 'epsilon', 'phi'
            
        Output:
            psi_final -- batch of final states; shape=[batch_size,N]
            psi_cached -- batch of cached states; shape=[batch_size,N]
            obs -- measurement outcomes; shape=[batch_size,1]
            
        """
        # extract parameters
        alpha = self.vec_to_complex(action['alpha'])
        beta = self.vec_to_complex(action['beta'])
        epsilon = self.vec_to_complex(action['epsilon'])
        phi = action['phi']
        
        Kraus = {}
        T = {}
        T['a'] = self.translate(alpha)
        T['+b'] = self.translate(beta/2.0)
        T['-b'] = tf.linalg.adjoint(T['+b'])
        T['+e'] = self.translate(epsilon/2.0)
        T['-e'] = tf.linalg.adjoint(T['+e'])

        
        chunk1 = 1j*batch_dot(T['-b'], batch_dot(T['+e'], T['+b'])) \
                - 1j*batch_dot(T['-b'], batch_dot(T['-e'], T['+b'])) \
                + batch_dot(T['-b'], batch_dot(T['-e'], T['-b'])) \
                + batch_dot(T['-b'], batch_dot(T['+e'], T['-b']))
                    
        chunk2 = 1j*batch_dot(T['+b'], batch_dot(T['-e'], T['-b'])) \
                - 1j*batch_dot(T['+b'], batch_dot(T['+e'], T['-b'])) \
                + batch_dot(T['+b'], batch_dot(T['-e'], T['+b'])) \
                + batch_dot(T['+b'], batch_dot(T['+e'], T['+b']))
        
        Kraus[0] = 1/4*(chunk1 + self.phase(phi)*chunk2)
        Kraus[1] = 1/4*(chunk1 - self.phase(phi)*chunk2)

        psi = self.mc_sim_delay.run(psi)
        psi_cached = batch_dot(T['a'], psi)
        psi = self.mc_sim_round.run(psi_cached)
        psi = self.normalize(psi)
        psi_final, obs = self.measurement(psi, Kraus)
        
        return psi_final, psi_cached, obs


    @tf.function
    def phase_estimation(self, psi, beta, angle, sample = False):
        """
        One round of phase estimation. 
        
        Input:
            psi -- batch of state vectors; shape=[batch_size,N]
            beta -- translation amplitude. shape=(batch_size,)
            angle -- angle along which to measure qubit. shape=(batch_size,)
        
        Output:
            psi -- batch of collapsed states if sample==True, otherwise same 
                   as input psi; shape=[batch_size,N]
            z -- batch of measurement outcomes if sample==True, otherwise
                 batch of expectation values of qubit sigma_z.
                 
        """
        Kraus = {}
        I = tf.stack([self.I]*self.batch_size)
        T_b = self.translate(beta)
        Kraus[0] = 1/2*(I + self.phase(angle)*T_b)
        Kraus[1] = 1/2*(I - self.phase(angle)*T_b)
        
        psi = self.normalize(psi)
        if sample:
            return self.measurement(psi, Kraus)
        else:
            # TODO: this can be done in 'measurement', pass 'sample' flag
            collapsed, p = {}, {}
            for i in [0,1]:
                collapsed[i] = batch_dot(Kraus[i], psi)
                p[i] = batch_dot(tf.math.conj(collapsed[i]), collapsed[i])
                p[i] = tf.math.real(p[i])
            return psi, p[0]-p[1] # expectation of sigma_z    


    ### GATES


    @tf.function
    def phase(self, phi):
        """
        Batch phase factor.
        
        Input:
            phi -- tensor of shape (batch_size,) or compatible

        Output:
            op -- phase factor; shape=[batch_size,1,1]
            
        """
        phi = tf.cast(phi, dtype=c64)
        phi = tf.reshape(phi, shape=[self.batch_size,1,1])        
        op = tf.linalg.expm(1j*phi)
        return op


    @tf.function
    def translate(self, amplitude):
        """
        Batch oscillator translation operator. 
        
        Input:
            amplitude -- tensor of shape (batch_size,) or compatible
            
        Output:
            op -- translation operator; shape=[batch_size,N,N]

        """
        a = tf.stack([self.a]*self.batch_size)
        a_dag = tf.stack([self.a_dag]*self.batch_size)
        amplitude = tf.reshape(amplitude, shape=[self.batch_size,1,1])
        batch = amplitude/sqrt(2)*a_dag - tf.math.conj(amplitude)/sqrt(2)*a
        op = tf.linalg.expm(batch)
        return op

    