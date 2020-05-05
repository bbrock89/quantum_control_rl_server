# -*- coding: utf-8 -*-
"""
Created on Thu Feb 20 14:50:22 2020

@author: Vladimir Sivak
"""

import numpy as np
import qutip as qt
import tensorflow as tf
import tensorflow_probability as tfp
from tensorflow import complex64 as c64
from tensorflow.keras.backend import batch_dot
from math import pi, sqrt
from tf_agents import specs
from tf_agents.environments import tf_environment
from tf_agents.trajectories import time_step as ts
from tf_agents.specs import tensor_spec

from gkp.gkp_tf_env import helper_functions as hf
from gkp.gkp_tf_env import config
from gkp.gkp_tf_env.tf_quantum_trajectory import QuantumTrajectorySim


class GKP(tf_environment.TFEnvironment):
    """
    Custom environment that follows TensorFlow Agents interface and allows to 
    train a reinforcement learning agent to find optimal measurement-based 
    feedback protocol for GKP-state stabilization.
    
    This implementation heavily relies on tensorflow to do fast computations 
    in parallel on GPU by adding batch dimension to all tensors. The speedup
    over all-qutip implementation is about x100 on NVIDIA RTX 2080Ti.
    
    Actions are parametrized according to the sequence of gates applied at 
    each time step, see <quantum_circuit_v1>, <...v2> or <...v3> 
    
    In <quantum_circuit_v1> and <...v2> each action is a 5-vector 
    [Re(alpha), Im(alpha), Re(beta), Im(beta), phi], where 'alpha' and 'beta' 
    are feedback and controlled-translation amplitudes, and 'phi' is qubit 
    measurement angle in the phase estimation circuit. Observations are qubit 
    sigma_z measurement outcomes from the set {-1,1}. 
    In <quantum_circuit_v3> additional action dimensions Re(eps) and Im(eps) 
    are added for trimming of GKP envelope, thus each action is a 7-vector.
    
    Environment step() method returns TimeStep tuple whose 'observation' 
    attribute stores the finite-horizon history of applied actions, measurement 
    outcomes and state wavefunctions. User needs to define a wrapper for the 
    environment if some components of this observation are to be discarded.
    
    """
    def __init__(self, **kwargs):
        # Load parameters of oscillator-qubit system
        params = [p for p in config.__dict__ if '__' not in p]
        for param in params:
            setattr(self, param, config.__getattribute__(param))

        # Default simulation parameters
        self.N = 100 # size of the oscillator Hilbert space truncation
        self.H = 1   # horizon for history returned in observations
        self.max_episode_length = None
        self.episode_length = 20
        self.batch_size = 50
        self.init = 'vac'
        self.reward_mode = 'stabilizers'
        self.quantum_circuit_type = 'v1'

        # Overwrite defaults if any, e.g. init, reward_mode, etc
        for key, val in kwargs.items():
            setattr(self, key, val)
        
        # Define action and observation specs
        action_spec, time_step_spec = self.create_specs()
        super(GKP, self).__init__(time_step_spec, action_spec, self.batch_size)

        # create all tensors
        self.define_operators()
        self.init_monte_carlo_sim()
        S = np.array([[1, 0],
                      [0, 1]])
        self.define_stabilizer_code(S)
        
        
    def create_specs(self):
        """
        Depending on the 'quantum_circuit_type', create action and time_step
        specs required by parent class. 
        
        """
        # Create action spec
        spec = lambda x,y: specs.TensorSpec(shape=[x,y], dtype=tf.float32)
        action_spec = {
            'alpha' : spec(1,2), 
            'beta'  : spec(1,2), 
            'phi'   : spec(1,1)}
        if self.quantum_circuit_type == 'v3': 
            action_spec['epsilon'] = spec(1,2)

        # Create time step spec
        observation_spec = {
            'alpha' : spec(self.H, 2), 
            'beta'  : spec(self.H, 2), 
            'phi'   : spec(self.H, 1),
            'msmt'  : spec(self.H, 1)}
        if self.quantum_circuit_type == 'v3': 
            observation_spec['epsilon'] = spec(self.H, 2)
        time_step_spec = ts.time_step_spec(observation_spec)

        self.quantum_circuit = self.__getattribute__(
            'quantum_circuit_' + self.quantum_circuit_type)
        
        return action_spec, time_step_spec
        

    ### STANDARD


    def _step(self, action):
        """
        Execute one time step in the environment.
        
        Input:
            action -- dictionary of batched actions
        
        Output:
            TimeStep object (see tf-agents docs)  
            
        """
        self._state, info, obs = self.quantum_circuit(self._state, action)
        self.info['psi_cached'] = info
        # Calculate rewards
        self._elapsed_steps += 1
        self._episode_ended = (self._elapsed_steps == self.episode_length)
        
        # Add dummy time dimension to tensors and append them to history
        for a in action.keys():
            self.history[a].append(tf.expand_dims(action[a], axis=1))
        self.history['msmt'].append(tf.expand_dims(obs, axis=1))
        
        # Make observations of horizon H, shape=[batch_size,H,dim]
        observation = {key : tf.concat(val[-self.H:], axis=1) 
                       for key, val in self.history.items()}

        reward = self.calculate_reward(obs, action)
        self._episode_return += reward
        
        if self._episode_ended:
            self._current_time_step_ = ts.termination(observation, reward)
        else:
            self._current_time_step_ = ts.transition(observation, reward)
        return self.current_time_step()


    def _reset(self):
        """
        Reset the state of the environment to an initial state. States are 
        represented as batched tensors. 
        
        Input:
            init -- type of states to create in a batch
                * 'vac': vacuum state
                * 'X+','X-','Y+','Y-','Z+','Z-': one of the cardinal states
                * 'random': sample batch of random states from 'X+','Y+','Z+'
                
        Output:
            TimeStep object (see tf-agents docs)
            
        """        
        if self.init in ['vac','X+','X-','Y+','Y-','Z+','Z-']:
            psi = self.states[self.init]
            psi_batch = tf.stack([psi]*self.batch_size)
            self._state = psi_batch
            self._original = np.array([self.init]*self.batch_size)
        elif self.init == 'random':
            self._original = np.random.choice(['X+','Y+','Z+'], 
                                              size=self.batch_size)
            psi_batch = [self.states[init] for init in self._original]
            psi_batch = tf.convert_to_tensor(psi_batch, dtype=c64)
            self._state = psi_batch
        
        if self.max_episode_length:
            self.episode_length = np.random.randint(1,self.max_episode_length)
        self._episode_ended = False
        self._elapsed_steps = 0
        self._episode_return = 0
        self.info = {} # use to cache some intermediate results

        # Initialize history of horizon H with actions=0 and measurements=1 
        self.history = tensor_spec.zero_spec_nest(self.action_spec(), 
                                      outer_dims=(self.batch_size,))
        self.history['msmt'] = tf.ones(shape=[self.batch_size,1,1])
        for key in self.history.keys():
            self.history[key] = [self.history[key]]*self.H
        
        # Make observation of horizon H, shape=[batch_size,H,dim] of each
        observation = {key : tf.concat(val[-self.H:], axis=1) 
                       for key, val in self.history.items()}
        
        self._current_time_step_ = ts.restart(observation, self.batch_size)
        return self.current_time_step()    


    def render(self):
        """
        Render environment to the screen (plot Wigner function).
        
        """
        hf.plot_wigner_tf_wrapper(self._state, tensorstate=self.tensorstate)


    def _current_time_step(self):
        return self._current_time_step_
    

    ### GKP - SPECIFIC


    def define_stabilizer_code(self, S):
        """
        Create stabilizer tensors, logical Pauli tensors and GKP state tensors.
        The simulation Hilbert space consists of N levels of the oscillator 
        and, if the 'tensorstate' flag is set, it also includes the qubit. In 
        the latter case the qubit comes first in the tensor product. 
        
        Input:
            S   -- symplectic 2x2 matrix that defines the code subspace
            
        """
        stabilizers, pauli, states, self.code_map = \
            hf.GKP_state(self.tensorstate, self.N, S)
        # Convert to tensorflow tensors.
        self.stabilizers = {key : tf.constant(val.full(), dtype=c64)
                            for key, val in stabilizers.items()}
        self.pauli = {key : tf.constant(val.full(), dtype=c64)
                      for key, val in pauli.items()}
        self.states = {key : tf.squeeze(tf.constant(val.full(), dtype=c64))
                       for key, val in states.items()}
        vac = qt.basis(self.N,0)
        self.states['vac'] = tf.squeeze(tf.constant(vac.full(), dtype=c64))


    def init_monte_carlo_sim(self):
        """
        Initialize tensorflow quantum trajectory simulator. This is used to
        simulate decoherence, dephasing, Kerr etc using quantum jumps.
        
        """        
        # Create Kraus ops
        Kraus = {}
        dt = self.discrete_step_duration
        Kraus[0] = self.I - 1j*self.Hamiltonian*dt
        for i, c in enumerate(self.c_ops):
            Kraus[i+1] = sqrt(dt) * c
            Kraus[0] -= 1/2 * tf.linalg.matmul(c, c, adjoint_a=True) * dt
        
        Kraus_tf = {}
        for i, op in Kraus.items():
            Kraus_tf[i] = tf.stack([op]*self.batch_size)
        
        # Initialize quantum trajectories simulator 
        mc_steps_round = int((self.t_gate + self.t_read) / dt)
        self.mc_sim_round = QuantumTrajectorySim(Kraus_tf, mc_steps_round)
        self.mc_sim_round.run = tf.function(self.mc_sim_round.run)

        mc_steps_delay = int(self.t_delay / dt)
        self.mc_sim_delay = QuantumTrajectorySim(Kraus_tf, mc_steps_delay)
        self.mc_sim_delay.run = tf.function(self.mc_sim_delay.run)


    @tf.function
    def normalize(self, state):
        """
        Batch normalization of the wave function.
        
        Input:
            state -- batch of state vectors; shape=[batch_size,NH]
            
        """
        norm = tf.math.real(batch_dot(tf.math.conj(state),state))
        norm = tf.cast(tf.math.sqrt(norm), dtype=c64)
        state = state / norm
        return state     


    @tf.function
    def vec_to_complex(self, a):
        """
        Convert vectorized action of shape [batch_sized,2] to complex-valued
        action of shape (batch_sized,)
        
        """
        return tf.cast(a[:,0], c64) + 1j*tf.cast(a[:,1], c64)


    @tf.function
    def measurement(self, psi, Kraus):
        """
        Batch measurement projection.
        
        Input:
            psi -- batch of states; shape=[batch_size, NH]
            Kraus -- dictionary of Kraus operators corresponding to 2 different 
                     qubit measurement outcomes. Shape of each operator is 
                     [b,NH,NH], where b is batch size
            
        Output:
            psi -- batch of collapsed states; shape=[batch_size,NH]
            obs -- measurement outcomes; shape=[batch_size,1]
            
        """    
        collapsed, p = {}, {}
        for i in Kraus.keys():
            collapsed[i] = batch_dot(Kraus[i], psi)
            p[i] = batch_dot(tf.math.conj(collapsed[i]), collapsed[i])
            p[i] = tf.math.real(p[i])
            
        obs = tfp.distributions.Bernoulli(probs=p[1]/(p[0]+p[1])).sample()
        mask = tf.cast(obs, dtype=c64)
        psi = collapsed[0] * (1-mask) + collapsed[1] * mask
        obs = 1 - 2*obs # convert to {-1,1}
        obs = tf.cast(obs, dtype=tf.float32)
        return psi, obs


    ### REWARD FUNCTION


    @tf.function
    def reward_zero(self, obs, act):
        """
        Reward is always zero (use when not training).
        
        """
        return tf.zeros(self.batch_size, dtype=tf.float32)
    
    # TODO: remove arguments from all reward functions once this is obsolete
    @tf.function
    def reward_stabilizers(self, obs, act):
        """
        Reward for stabilizing GKP subspace, i.e. having both stabilizers
        equal 1. Use this reward only if each time step is doing phase 
        estimation on one of the stabilizers and with phi=0 which correponds 
        to measuring real part of the stabilizer eigenvalue.

        Input:
            obs -- observations at this time step; shape=(batch_size,)
            act -- actions at this time step; shape=(batch_size,act_dim)
            
        """        
        mask = tf.math.equal(act['phi'][:,0], 0.0)
        mask = tf.cast(mask, tf.float32)
        z = tf.reshape(obs, shape=(self.batch_size,))*mask
        return z


    def reward_pauli(self, obs, act):
        """
        Reward only on last time step with the result of measurement of logical
        Pauli operator using cached wavefunction (after feedback translation).
        Such reward lets the agent directly optimize T1.

        Input:
            obs -- observations at this time step; shape=(batch_size,)
            act -- actions at this time step; shape=(batch_size,act_dim)
            
        """     
        if self._elapsed_steps < self.episode_length:
            z = tf.zeros(self.batch_size, dtype=tf.float32)
        else:
            pauli = [self.code_map[self._original[i][0]] 
                         for i in range(self.batch_size)]
            pauli = tf.convert_to_tensor(pauli, dtype=c64)
            phi = tf.zeros(self.batch_size)
            _, z = self.phase_estimation(self.info['psi_cached'], pauli, 
                                         angle=phi, sample=True)
            z = tf.cast(z, dtype=tf.float32)
            z = tf.reshape(z, shape=(self.batch_size,))
        return z

    def reward_mixed(self, obs, act):
        """
        Reward only on last time step with the result of measurement of 
        randomly selected stabilizer. This reinforces high degree of 
        squeezing in the GKP subspace.

        Input:
            obs -- observations at this time step; shape=(batch_size,)
            act -- actions at this time step; shape=(batch_size,act_dim)
            
        """
        if self._elapsed_steps < self.episode_length:
            z = tf.zeros(self.batch_size, dtype=tf.float32)
        else:        
            mask = tfp.distributions.Bernoulli(probs=[0.5]*self.batch_size, 
                                               dtype=tf.float32).sample()
            beta = self.code_map['S_q']*mask + self.code_map['S_p']*(1-mask)
            beta = tf.cast(beta, dtype=c64)
            phi = tf.zeros(self.batch_size)
            _, z = self.phase_estimation(self.info['psi_cached'], beta, 
                                         angle=phi, sample=True)
            z = tf.cast(z, dtype=tf.float32)
            z = tf.reshape(z, shape=(self.batch_size,))
        return z


    ### PROPERTIES
    
    
    @property
    def reward_mode(self):
        return self._reward_mode

    @reward_mode.setter
    def reward_mode(self, mode):
        try:
            assert mode in ['zero', 'pauli', 'stabilizers', 'mixed']
            self._reward_mode = mode
            if mode == 'zero':
                self.calculate_reward = self.reward_zero
            if mode == 'stabilizers':
                self.calculate_reward = self.reward_stabilizers
            if mode == 'pauli':
                if self.init == 'vac':
                    raise Exception('Pauli reward not supported for vac')
                self.calculate_reward = self.reward_pauli
            if mode == 'mixed':
                self.calculate_reward = self.reward_mixed
        except: 
            raise ValueError('Reward mode not supported.') 
    
    @property
    def init(self):
        return self._init
    
    @init.setter
    def init(self, val):
        try:
            assert val in ['vac','random','X+','X-','Y+','Y-','Z+','Z-']
            self._init = val
        except:
            raise ValueError('Initial state not supported.')
    
    @property
    def batch_size(self):
        return self._batch_size

    @batch_size.setter
    def batch_size(self, size):
        if 'code_map' in self.__dir__():
            raise ValueError('Cannot change batch_size after initialization.')
        try:
            assert size>0 and isinstance(size,int)
            self._batch_size = size
        except:
            raise ValueError('Batch size should be positive integer.')
    
    @property 
    def N(self):
        return self._N
    
    @N.setter
    def N(self, n):
        if 'code_map' in self.__dir__():
            raise ValueError('Cannot change N after initialization.')
        else:
            self._N = n
    
    
    
    