import os
import torch
import numpy as np
import numpy.random as rd
from copy import deepcopy
from ray_elegantrl.net import QNet, QNetDuel, QNetTwin, QNetTwinDuel
from ray_elegantrl.net import Actor, ActorSAC, ActorPPO, ActorMPO
from ray_elegantrl.net import Critic, CriticAdv, CriticTwin
from scipy.optimize import minimize
from IPython import embed

"""
Modify [ElegantRL](https://github.com/AI4Finance-Foundation/ElegantRL)
by https://github.com/GyChou
"""


class AgentBase:
    def __init__(self, args=None):
        self.learning_rate = 1e-4 if args is None else args['learning_rate']
        self.soft_update_tau = 2 ** -8 if args is None else args['soft_update_tau']  # 5e-3 ~= 2 ** -8
        self.state = None  # set for self.update_buffer(), initialize before training
        self.device = None

        self.act = self.act_target = None
        self.cri = self.cri_target = None
        self.act_optimizer = None
        self.cri_optimizer = None
        self.criterion = None
        self.get_obj_critic = None
        self.train_record = {}

    def init(self, net_dim, state_dim, action_dim, if_per=False):
        """initialize the self.object in `__init__()`

        replace by different DRL algorithms
        explict call self.init() for multiprocessing.

        `int net_dim` the dimension of networks (the width of neural networks)
        `int state_dim` the dimension of state (the number of state vector)
        `int action_dim` the dimension of action (the number of discrete action)
        `bool if_per` Prioritized Experience Replay for sparse reward
        """

    def select_action(self, state) -> np.ndarray:
        """Select actions for exploration

        :array state: state.shape==(state_dim, )
        :return array action: action.shape==(action_dim, ), (action.min(), action.max())==(-1, +1)
        """
        states = torch.as_tensor((state,), dtype=torch.float32, device=self.device).detach_()
        action = self.act(states)[0]
        return action.cpu().numpy()

    def explore_env(self, env, buffer, target_step, reward_scale, gamma) -> int:
        """actor explores in env, then stores the env transition to ReplayBuffer

        :env: RL training environment. env.reset() env.step()
        :buffer: Experience Replay Buffer. buffer.append_buffer() buffer.extend_buffer()
        :int target_step: explored target_step number of step in env
        :float reward_scale: scale reward, 'reward * reward_scale'
        :float gamma: discount factor, 'mask = 0.0 if done else gamma'
        :return int target_step: collected target_step number of step in env
        """
        for _ in range(target_step):
            action = self.select_action(self.state)
            next_s, reward, done, _ = env.step(action)
            other = (reward * reward_scale, 0.0 if done else gamma, *action)
            buffer.append_buffer(self.state, other)
            self.state = env.reset() if done else next_s
        return target_step

    def update_net(self, buffer, target_step, batch_size, repeat_times) -> (float, float):
        """update the neural network by sampling batch data from ReplayBuffer

        replace by different DRL algorithms.
        return the objective value as training information to help fine-tuning

        `buffer` Experience replay buffer. buffer.append_buffer() buffer.extend_buffer()
        :int target_step: explore target_step number of step in env
        `int batch_size` sample batch_size of data for Stochastic Gradient Descent
        :float repeat_times: the times of sample batch = int(target_step * repeat_times) in off-policy
        :return float obj_a: the objective value of actor
        :return float obj_c: the objective value of critic
        """

    def save_load_model(self, cwd, if_save):
        """save or load model files

        :str cwd: current working directory, we save model file here
        :bool if_save: save model or load model
        """
        act_save_path = '{}/actor.pth'.format(cwd)
        cri_save_path = '{}/critic.pth'.format(cwd)

        def load_torch_file(network, save_path):
            network_dict = torch.load(save_path, map_location=lambda storage, loc: storage)
            network.load_state_dict(network_dict)

        if if_save:
            if self.act is not None:
                torch.save(self.act.state_dict(), act_save_path)
            if self.cri is not None:
                torch.save(self.cri.state_dict(), cri_save_path)
        elif (self.act is not None) and os.path.exists(act_save_path):
            load_torch_file(self.act, act_save_path)
            print("Loaded act:", cwd)
        elif (self.cri is not None) and os.path.exists(cri_save_path):
            load_torch_file(self.cri, cri_save_path)
            print("Loaded cri:", cwd)
        else:
            print("FileNotFound when load_model: {}".format(cwd))

    @staticmethod
    def soft_update(target_net, current_net, tau):
        """soft update a target network via current network

        :nn.Module target_net: target network update via a current network, it is more stable
        :nn.Module current_net: current network update via an optimizer
        """
        for tar, cur in zip(target_net.parameters(), current_net.parameters()):
            tar.data.copy_(cur.data.__mul__(tau) + tar.data.__mul__(1 - tau))

    def update_record(self, **kwargs):
        """update the self.train_record for recording the metrics in training process
        :**kwargs :named arguments is the metrics name, arguments value is the metrics value.
        both of them will be prined and showed in tensorboard
        """
        self.train_record.update(kwargs)


'''Value-based Methods (DQN variances)'''


class AgentDQN(AgentBase):
    def __init__(self):
        super().__init__()
        self.explore_rate = 0.1  # the probability of choosing action randomly in epsilon-greedy
        self.action_dim = None  # chose discrete action randomly in epsilon-greedy

    def init(self, net_dim, state_dim, action_dim, if_per=False):
        self.action_dim = action_dim
        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

        self.cri = QNet(net_dim, state_dim, action_dim).to(self.device)
        self.cri_target = deepcopy(self.cri)
        self.cri_optimizer = torch.optim.Adam(self.cri.parameters(), lr=self.learning_rate)
        self.act = self.cri  # to keep the same from Actor-Critic framework

        self.criterion = torch.nn.MSELoss(reduction='none' if if_per else 'mean')
        if if_per:
            self.get_obj_critic = self.get_obj_critic_per
        else:
            self.get_obj_critic = self.get_obj_critic_raw

    def select_action(self, state) -> int:  # for discrete action space
        if rd.rand() < self.explore_rate:  # epsilon-greedy
            a_int = rd.randint(self.action_dim)  # choosing action randomly
        else:
            states = torch.as_tensor((state,), dtype=torch.float32, device=self.device).detach_()
            action = self.act(states)[0]
            a_int = action.argmax(dim=0).cpu().numpy()
        return a_int

    def explore_env(self, env, buffer, target_step, reward_scale, gamma) -> int:
        for _ in range(target_step):
            action = self.select_action(self.state)
            next_s, reward, done, _ = env.step(action)

            other = (reward * reward_scale, 0.0 if done else gamma, action)  # action is an int
            buffer.append_buffer(self.state, other)
            self.state = env.reset() if done else next_s
        return target_step

    def update_net(self, buffer, target_step, batch_size, repeat_times) -> (float, float):
        buffer.update_now_len_before_sample()

        q_value = obj_critic = None
        for _ in range(int(target_step * repeat_times)):
            obj_critic, q_value = self.get_obj_critic(buffer, batch_size)

            self.cri_optimizer.zero_grad()
            obj_critic.backward()
            self.cri_optimizer.step()
            self.soft_update(self.cri_target, self.cri, self.soft_update_tau)
        self.update_record(obj_a=q_value.mean().item(), obj_c=obj_critic.item())
        return self.train_record

    def get_obj_critic_raw(self, buffer, batch_size):
        with torch.no_grad():
            reward, mask, action, state, next_s = buffer.sample_batch(batch_size)
            next_q = self.cri_target(next_s, self.act_target(next_s))
            q_label = reward + mask * next_q

        q_value = self.cri(state).gather(1, action.type(torch.long))
        obj_critic = self.criterion(q_value, q_label)
        return obj_critic, q_value

    def get_obj_critic_per(self, buffer, batch_size):
        with torch.no_grad():
            reward, mask, action, state, next_s, is_weights = buffer.sample_batch(batch_size)
            next_q = self.cri_target(next_s, self.act_target(next_s))
            q_label = reward + mask * next_q

        q_value = self.cri(state).gather(1, action.type(torch.long))
        obj_critic = (self.criterion(q_value, q_label) * is_weights).mean()
        return obj_critic, q_value


class AgentDuelingDQN(AgentDQN):
    def __init__(self):
        super().__init__()
        self.explore_rate = 0.25  # the probability of choosing action randomly in epsilon-greedy

    def init(self, net_dim, state_dim, action_dim, if_per=False):
        self.action_dim = action_dim
        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

        self.cri = QNetDuel(net_dim, state_dim, action_dim).to(self.device)
        self.cri_target = deepcopy(self.cri)
        self.act = self.cri

        self.cri_optimizer = torch.optim.Adam(self.cri.parameters(), lr=self.learning_rate)
        self.criterion = torch.nn.MSELoss(reduction='none' if if_per else 'mean')
        self.get_obj_critic = self.get_obj_critic_per if if_per else self.get_obj_critic_raw


class AgentDoubleDQN(AgentDQN):
    def __init__(self):
        super().__init__()
        self.explore_rate = 0.25  # the probability of choosing action randomly in epsilon-greedy
        self.softmax = torch.nn.Softmax(dim=1)

    def init(self, net_dim, state_dim, action_dim, if_per=False):
        self.action_dim = action_dim
        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

        self.cri = QNetTwin(net_dim, state_dim, action_dim).to(self.device)
        self.cri_target = deepcopy(self.cri)
        self.cri_optimizer = torch.optim.Adam(self.act.parameters(), lr=self.learning_rate)

        self.act = self.cri
        self.criterion = torch.nn.SmoothL1Loss(reduction='none' if if_per else 'mean')
        self.get_obj_critic = self.get_obj_critic_per if if_per else self.get_obj_critic_raw

    def select_action(self, state) -> int:  # for discrete action space
        states = torch.as_tensor((state,), dtype=torch.float32, device=self.device).detach_()
        actions = self.act(states)
        if rd.rand() < self.explore_rate:  # epsilon-greedy
            action = self.softmax(actions)[0]
            a_prob = action.detach().cpu().numpy()  # choose action according to Q value
            a_int = rd.choice(self.action_dim, p=a_prob)
        else:
            action = actions[0]
            a_int = action.argmax(dim=0).cpu().numpy()
        return a_int

    def get_obj_critic_raw(self, buffer, batch_size):
        with torch.no_grad():
            reward, mask, action, state, next_s = buffer.sample_batch(batch_size)
            next_q = torch.min(*self.cri_target.get_q1_q2(next_s))
            next_q = next_q.max(dim=1, keepdim=True)[0]
            q_label = reward + mask * next_q
        act_int = action.type(torch.long)
        q1, q2 = [qs.gather(1, act_int) for qs in self.act.get_q1_q2(state)]
        obj_critic = self.criterion(q1, q_label) + self.criterion(q2, q_label)
        return obj_critic, q1

    def get_obj_critic_per(self, buffer, batch_size):
        with torch.no_grad():
            reward, mask, action, state, next_s, is_weights = buffer.sample_batch(batch_size)
            next_q = torch.min(*self.cri_target.get_q1_q2(next_s))
            next_q = next_q.max(dim=1, keepdim=True)[0]
            q_label = reward + mask * next_q
        act_int = action.type(torch.long)
        q1, q2 = [qs.gather(1, act_int) for qs in self.act.get_q1_q2(state)]
        obj_critic = ((self.criterion(q1, q_label) + self.criterion(q2, q_label)) * is_weights).mean()
        return obj_critic, q1


class AgentD3QN(AgentDoubleDQN):  # D3QN: Dueling Double DQN
    def __init__(self):
        super().__init__()

    def init(self, net_dim, state_dim, action_dim, if_per=False):
        self.action_dim = action_dim
        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

        self.cri = QNetTwinDuel(net_dim, state_dim, action_dim).to(self.device)
        self.cri_target = deepcopy(self.cri)
        self.act = self.cri

        self.criterion = torch.nn.SmoothL1Loss(reduction='none') if if_per else torch.nn.SmoothL1Loss()
        self.cri_optimizer = torch.optim.Adam(self.act.parameters(), lr=self.learning_rate)
        self.criterion = torch.nn.SmoothL1Loss(reduction='none' if if_per else 'mean')
        self.get_obj_critic = self.get_obj_critic_per if if_per else self.get_obj_critic_raw


'''Actor-Critic Methods (Policy Gradient)'''


class AgentDDPG(AgentBase):
    def __init__(self):
        super().__init__()
        self.ou_explore_noise = 0.3  # explore noise of action
        self.ou_noise = None

    def init(self, net_dim, state_dim, action_dim, if_per=False):
        self.ou_noise = OrnsteinUhlenbeckNoise(size=action_dim, sigma=self.ou_explore_noise)
        # I don't recommend to use OU-Noise
        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

        self.cri = Critic(net_dim, state_dim, action_dim).to(self.device)
        self.cri_target = deepcopy(self.cri)
        self.cri_optimizer = torch.optim.Adam(self.cri.parameters(), lr=self.learning_rate)

        self.act = Actor(net_dim, state_dim, action_dim).to(self.device)
        self.act_target = deepcopy(self.act)
        self.act_optimizer = torch.optim.Adam(self.act.parameters(), lr=self.learning_rate)

        self.criterion = torch.nn.SmoothL1Loss(reduction='none' if if_per else 'mean')
        if if_per:
            self.get_obj_critic = self.get_obj_critic_per
        else:
            self.get_obj_critic = self.get_obj_critic_raw

    def select_action(self, state) -> np.ndarray:
        states = torch.as_tensor((state,), dtype=torch.float32, device=self.device).detach_()
        action = self.act(states)[0].cpu().numpy()
        return (action + self.ou_noise()).ratio_clip(-1, 1)

    def update_net(self, buffer, target_step, batch_size, repeat_times) -> (float, float):
        buffer.update_now_len_before_sample()

        obj_critic = obj_actor = None  # just for print return
        for _ in range(int(target_step * repeat_times)):
            obj_critic, state = self.get_obj_critic(buffer, batch_size)
            self.cri_optimizer.zero_grad()
            obj_critic.backward()
            self.cri_optimizer.step()
            self.soft_update(self.cri_target, self.cri, self.soft_update_tau)

            q_value_pg = self.act(state)  # policy gradient
            obj_actor = -self.cri_target(state, q_value_pg).mean()  # obj_actor
            self.act_optimizer.zero_grad()
            obj_actor.backward()
            self.act_optimizer.step()
            self.soft_update(self.act_target, self.act, self.soft_update_tau)
        self.update_record(obj_a=obj_actor.item(), obj_c=obj_critic.item())
        return self.train_record

    def get_obj_critic_raw(self, buffer, batch_size):
        with torch.no_grad():
            reward, mask, action, state, next_s = buffer.sample_batch(batch_size)
            next_q = self.cri_target(next_s, self.act_target(next_s))
            q_label = reward + mask * next_q
        q_value = self.cri(state, action)
        obj_critic = self.criterion(q_value, q_label)
        return obj_critic, state

    def get_obj_critic_per(self, buffer, batch_size):
        with torch.no_grad():
            reward, mask, action, state, next_s, is_weights = buffer.sample_batch(batch_size)
            next_q = self.cri_target(next_s, self.act_target(next_s))
            q_label = reward + mask * next_q
        q_value = self.cri(state, action)
        obj_critic = (self.criterion(q_value, q_label) * is_weights).mean()

        td_error = (q_label - q_value.detach()).abs()
        buffer.td_error_update(td_error)
        return obj_critic, state


class AgentTD3(AgentDDPG):
    def __init__(self):
        super().__init__()
        self.explore_noise = 0.1  # standard deviation of explore noise
        self.policy_noise = 0.2  # standard deviation of policy noise
        self.update_freq = 2  # delay update frequency, for soft target update

    def init(self, net_dim, state_dim, action_dim, if_per=False):
        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

        self.cri = CriticTwin(net_dim, state_dim, action_dim).to(self.device)
        self.cri_target = deepcopy(self.cri)
        self.cri_optimizer = torch.optim.Adam(self.cri.parameters(), lr=self.learning_rate)

        self.act = Actor(net_dim, state_dim, action_dim).to(self.device)
        self.act_target = deepcopy(self.act)
        self.act_optimizer = torch.optim.Adam(self.act.parameters(), lr=self.learning_rate)

        self.criterion = torch.nn.SmoothL1Loss(reduction='none' if if_per else 'mean')
        if if_per:
            self.get_obj_critic = self.get_obj_critic_per
        else:
            self.get_obj_critic = self.get_obj_critic_raw

    def select_action(self, state) -> np.ndarray:
        states = torch.as_tensor((state,), dtype=torch.float32, device=self.device).detach_()
        action = self.act(states)[0]
        action = (action + torch.randn_like(action) * self.explore_noise).clamp(-1, 1)
        return action.cpu().numpy()

    def update_net(self, buffer, target_step, batch_size, repeat_times) -> (float, float):
        buffer.update_now_len_before_sample()

        obj_critic = obj_actor = None
        for i in range(int(target_step * repeat_times)):
            obj_critic, state = self.get_obj_critic(buffer, batch_size)
            self.cri_optimizer.zero_grad()
            obj_critic.backward()
            self.cri_optimizer.step()
            if i % self.update_freq == 0:  # delay update
                self.soft_update(self.cri_target, self.cri, self.soft_update_tau)

            q_value_pg = self.act(state)  # policy gradient
            obj_actor = -self.cri_target(state, q_value_pg).mean()  # obj_actor
            self.act_optimizer.zero_grad()
            obj_actor.backward()
            self.act_optimizer.step()
            if i % self.update_freq == 0:  # delay update
                self.soft_update(self.act_target, self.act, self.soft_update_tau)

        self.update_record(obj_a=obj_actor.item(), obj_c=obj_critic.item() / 2)
        return self.train_record

    def get_obj_critic_raw(self, buffer, batch_size):
        with torch.no_grad():
            reward, mask, action, state, next_s = buffer.sample_batch(batch_size)
            next_a = self.act_target.get_action(next_s, self.policy_noise)  # policy noise
            next_q = torch.min(*self.cri_target.get_q1_q2(next_s, next_a))  # twin critics
            q_label = reward + mask * next_q
        q1, q2 = self.cri.get_q1_q2(state, action)
        obj_critic = self.criterion(q1, q_label) + self.criterion(q2, q_label)  # twin critics
        return obj_critic, state

    def get_obj_critic_per(self, buffer, batch_size):
        """Prioritized Experience Replay

        Contributor: Github GyChou
        """
        with torch.no_grad():
            reward, mask, action, state, next_s, is_weights = buffer.sample_batch(batch_size)
            next_a = self.act_target.get_action(next_s, self.policy_noise)  # policy noise
            next_q = torch.min(*self.cri_target.get_q1_q2(next_s, next_a))  # twin critics
            q_label = reward + mask * next_q

        q1, q2 = self.cri.get_q1_q2(state, action)
        obj_critic = ((self.criterion(q1, q_label) + self.criterion(q2, q_label)) * is_weights).mean()

        td_error = (q_label - torch.min(q1, q2).detach()).abs()
        buffer.td_error_update(td_error)
        return obj_critic, state


class AgentSAC(AgentBase):
    def __init__(self, args=None):
        super().__init__(args)
        self.alpha_log = None
        self.alpha_optimizer = None
        # * np.log(action_dim)
        self.target_entropy = 1.0

    def init(self, net_dim, state_dim, action_dim, reward_dim=1, if_per=False):
        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        self.target_entropy *= np.log(action_dim)
        self.alpha_log = torch.tensor((-np.log(action_dim) * np.e,), dtype=torch.float32,
                                      requires_grad=True, device=self.device)  # trainable parameter
        self.alpha_optimizer = torch.optim.Adam((self.alpha_log,), self.learning_rate)

        self.cri = CriticTwin(net_dim, state_dim, action_dim).to(self.device)
        self.cri_target = deepcopy(self.cri)
        self.cri_optimizer = torch.optim.Adam(self.cri.parameters(), lr=self.learning_rate)

        self.act = ActorSAC(net_dim, state_dim, action_dim).to(self.device)
        self.act_optimizer = torch.optim.Adam(self.act.parameters(), lr=self.learning_rate)

        self.criterion = torch.nn.SmoothL1Loss(reduction='none' if if_per else 'mean')
        if if_per:
            self.get_obj_critic = self.get_obj_critic_per
        else:
            self.get_obj_critic = self.get_obj_critic_raw

    @staticmethod
    def select_action(state, policy):
        states = torch.as_tensor((state,), dtype=torch.float32).detach_()
        action = policy.get_action(states)[0]
        return action.detach().numpy()

    def update_net(self, buffer, target_step, batch_size, repeat_times) -> (float, float):
        buffer.update_now_len_before_sample()

        alpha = self.alpha_log.exp().detach()
        obj_critic = None
        for _ in range(int(target_step * repeat_times)):
            '''objective of critic'''
            obj_critic, state = self.get_obj_critic(buffer, batch_size, alpha)
            self.cri_optimizer.zero_grad()
            obj_critic.backward()
            self.cri_optimizer.step()
            self.soft_update(self.cri_target, self.cri, self.soft_update_tau)

            '''objective of alpha (temperature parameter automatic adjustment)'''
            action_pg, logprob = self.act.get_action_logprob(state)  # policy gradient

            obj_alpha = (self.alpha_log * (logprob - self.target_entropy).detach()).mean()
            self.alpha_optimizer.zero_grad()
            obj_alpha.backward()
            self.alpha_optimizer.step()

            '''objective of actor'''
            alpha = self.alpha_log.exp().detach()
            obj_actor = -(torch.min(*self.cri_target.get_q1_q2(state, action_pg)) + logprob * alpha).mean()

            self.act_optimizer.zero_grad()
            obj_actor.backward()
            self.act_optimizer.step()

        self.update_record(obj_a=obj_actor.item(), obj_c=obj_critic.item())
        return self.train_record

    def get_obj_critic_raw(self, buffer, batch_size, alpha):
        with torch.no_grad():
            reward, mask, action, state, next_s = buffer.sample_batch(batch_size)
            next_a, next_logprob = self.act.get_action_logprob(next_s)
            next_q = torch.min(*self.cri_target.get_q1_q2(next_s, next_a))
            q_label = reward + mask * (next_q + next_logprob * alpha)
        q1, q2 = self.cri.get_q1_q2(state, action)  # twin critics
        obj_critic = self.criterion(q1, q_label) + self.criterion(q2, q_label)
        return obj_critic, state

    def get_obj_critic_per(self, buffer, batch_size, alpha):
        with torch.no_grad():
            reward, mask, action, state, next_s, is_weights = buffer.sample_batch(batch_size)
            next_a, next_logprob = self.act.get_action_logprob(next_s)
            next_q = torch.min(*self.cri_target.get_q1_q2(next_s, next_a))
            q_label = reward + mask * (next_q + next_logprob * alpha)
        q1, q2 = self.cri.get_q1_q2(state, action)  # twin critics
        obj_critic = ((self.criterion(q1, q_label) + self.criterion(q2, q_label)) * is_weights).mean()

        td_error = (q_label - torch.min(q1, q2).detach()).abs()
        buffer.td_error_update(td_error)
        return obj_critic, state


class AgentModSAC(AgentSAC):  # Modified SAC using reliable_lambda and TTUR (Two Time-scale Update Rule)
    def __init__(self, args=None):
        super().__init__(args)
        self.if_use_dn = True if args is None else args['if_use_dn']
        self.obj_c = (-np.log(0.5)) ** 0.5  # for reliable_lambda

    def init(self, net_dim, state_dim, action_dim, reward_dim=1, if_per=False):
        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        self.target_entropy *= np.log(action_dim)
        self.alpha_log = torch.tensor((-np.log(action_dim) * np.e,), dtype=torch.float32,
                                      requires_grad=True, device=self.device)  # trainable parameter
        self.alpha_optimizer = torch.optim.Adam((self.alpha_log,), self.learning_rate)

        self.cri = CriticTwin(net_dim, state_dim, action_dim, self.if_use_dn).to(self.device)
        self.cri_target = deepcopy(self.cri)
        self.cri_optimizer = torch.optim.Adam(self.cri.parameters(), self.learning_rate)

        self.act = ActorSAC(net_dim, state_dim, action_dim, self.if_use_dn).to(self.device)
        self.act_optimizer = torch.optim.Adam(self.act.parameters(), self.learning_rate)

        self.criterion = torch.nn.SmoothL1Loss(reduction='none' if if_per else 'mean')
        if if_per:
            self.get_obj_critic = self.get_obj_critic_per
        else:
            self.get_obj_critic = self.get_obj_critic_raw

    def update_net(self, buffer, target_step, batch_size, repeat_times) -> (float, float):
        buffer.update_now_len_before_sample()

        k = 1.0 + buffer.now_len / buffer.max_len
        batch_size_ = int(batch_size * k)
        train_steps = int(target_step * k * repeat_times)

        alpha = self.alpha_log.exp().detach()
        update_a = 0
        for update_c in range(1, train_steps):
            '''objective of critic (loss function of critic)'''
            obj_critic, state = self.get_obj_critic(buffer, batch_size_, alpha)
            self.obj_c = 0.995 * self.obj_c + 0.0025 * obj_critic.item()  # for reliable_lambda
            self.cri_optimizer.zero_grad()
            obj_critic.backward()
            self.cri_optimizer.step()
            self.soft_update(self.cri_target, self.cri, self.soft_update_tau)

            '''objective of alpha (temperature parameter automatic adjustment)'''
            action_pg, logprob = self.act.get_action_logprob(state)  # policy gradient

            obj_alpha = (self.alpha_log * (logprob - self.target_entropy).detach()).mean()
            self.alpha_optimizer.zero_grad()
            obj_alpha.backward()
            self.alpha_optimizer.step()

            with torch.no_grad():
                self.alpha_log[:] = self.alpha_log.clamp(-20, 2)
            alpha = self.alpha_log.exp().detach()

            '''objective of actor using reliable_lambda and TTUR (Two Time-scales Update Rule)'''
            reliable_lambda = np.exp(-self.obj_c ** 2)  # for reliable_lambda
            if_update_a = (update_a / update_c) < (1 / (2 - reliable_lambda))
            if if_update_a:  # auto TTUR
                update_a += 1

                q_value_pg = torch.min(*self.cri_target.get_q1_q2(state, action_pg))  # ceta3
                obj_actor = -(q_value_pg + logprob * alpha.detach()).mean()
                obj_actor = obj_actor * reliable_lambda  # max(0.01, reliable_lambda)

                self.act_optimizer.zero_grad()
                obj_actor.backward()
                self.act_optimizer.step()

        self.update_record(obj_a=obj_actor.item(), obj_c=self.obj_c)
        return self.train_record


class AgentPPO(AgentBase):
    def __init__(self, args=None):
        super().__init__(args)
        # could be 0.2 ~ 0.5, ratio.clamp(1 - clip, 1 + clip),
        self.ratio_clip = 0.3 if args is None else args['ratio_clip']
        # could be 0.01 ~ 0.05
        self.lambda_entropy = 0.05 if args is None else args['lambda_entropy']
        # could be 0.95 ~ 0.99, GAE (Generalized Advantage Estimation. ICLR.2016.)
        self.lambda_gae_adv = 0.97 if args is None else args['lambda_gae_adv']
        # if use Generalized Advantage Estimation
        self.if_use_gae = True if args is None else args['if_use_gae']
        # AgentPPO is an on policy DRL algorithm
        self.if_on_policy = True
        self.if_use_dn = False if args is None else args['if_use_dn']

        self.noise = None
        self.optimizer = None
        self.compute_reward = None  # attribution

    def init(self, net_dim, state_dim, action_dim, reward_dim=1, if_per=False):
        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        self.compute_reward = self.compute_reward_gae if self.if_use_gae else self.compute_reward_adv

        self.cri = CriticAdv(state_dim, net_dim, self.if_use_dn).to(self.device)
        self.act = ActorPPO(net_dim, state_dim, action_dim, self.if_use_dn).to(self.device)

        self.optimizer = torch.optim.Adam([{'params': self.act.parameters(), 'lr': self.learning_rate},
                                           {'params': self.cri.parameters(), 'lr': self.learning_rate}])
        self.criterion = torch.nn.SmoothL1Loss()
        assert if_per is False  # on-policy don't need PER

    @staticmethod
    def select_action(state, policy):
        """select action for PPO

       :array state: state.shape==(state_dim, )

       :return array action: state.shape==(action_dim, )
       :return array noise: noise.shape==(action_dim, ), the noise
       """
        states = torch.as_tensor((state,), dtype=torch.float32).detach_()
        action = policy.get_action(states)[0]
        return action.detach().numpy()

    def update_net(self, buffer, _target_step, batch_size, repeat_times=4) -> (float, float):
        buffer.update_now_len_before_sample()
        buf_len = buffer.now_len  # assert buf_len >= _target_step

        '''Trajectory using reverse reward'''
        with torch.no_grad():
            buf_reward, buf_mask, buf_action, buf_state = buffer.sample_all()

            bs = 2 ** 10  # set a smaller 'bs: batch size' when out of GPU memory.
            buf_value = torch.cat([self.cri(buf_state[i:i + bs]) for i in range(0, buf_state.size(0), bs)], dim=0)
            buf_logprob = self.act.compute_logprob(buf_state, buf_action).unsqueeze(dim=1)
            buf_r_ret, buf_adv = self.compute_reward(buf_len, buf_reward, buf_mask, buf_value)
            del buf_reward, buf_mask

        '''PPO: Surrogate objective of Trust Region'''
        obj_critic = None
        for _ in range(int(repeat_times * buf_len / batch_size)):
            indices = torch.randint(buf_len, size=(batch_size,), requires_grad=False, device=self.device)

            state = buf_state[indices]
            action = buf_action[indices]
            r_ret = buf_r_ret[indices]
            logprob = buf_logprob[indices]
            adv = buf_adv[indices]

            new_logprob = self.act.compute_logprob(state, action).unsqueeze(dim=1)  # it is obj_actor
            ratio = (new_logprob - logprob).exp()
            obj_surrogate1 = adv * ratio
            obj_surrogate2 = adv * ratio.clamp(1 - self.ratio_clip, 1 + self.ratio_clip)
            obj_surrogate = -torch.min(obj_surrogate1, obj_surrogate2).mean()
            obj_entropy = (new_logprob.exp() * new_logprob).mean()  # policy entropy
            obj_actor = obj_surrogate + obj_entropy * self.lambda_entropy

            value = self.cri(state)  # critic network predicts the reward_sum (Q value) of state
            obj_critic = self.criterion(value, r_ret)

            obj_united = obj_actor + obj_critic / (r_ret.std() + 1e-5)
            self.optimizer.zero_grad()
            obj_united.backward()
            self.optimizer.step()

        self.update_record(obj_a=obj_surrogate.item(),
                           obj_c=obj_critic.item(),
                           obj_tot=obj_united.item(),
                           a_std=self.act.a_std_log.exp().mean().item(),
                           entropy=(-obj_entropy.item()))
        return self.train_record

    def compute_reward_adv(self, buf_len, buf_reward, buf_mask, buf_value) -> (torch.Tensor, torch.Tensor):
        """compute the excepted discounted episode return

        :int buf_len: the length of ReplayBuffer
        :torch.Tensor buf_reward: buf_reward.shape==(buf_len, 1)
        :torch.Tensor buf_mask:   buf_mask.shape  ==(buf_len, 1)
        :torch.Tensor buf_value:  buf_value.shape ==(buf_len, 1)
        :return torch.Tensor buf_r_sum:      buf_r_sum.shape     ==(buf_len, 1)
        :return torch.Tensor buf_advantage:  buf_advantage.shape ==(buf_len, 1)
        """
        buf_r_ret = torch.empty(buf_reward.shape, dtype=torch.float32, device=self.device)  # reward sum
        pre_r_ret = torch.zeros(buf_reward.shape[1], dtype=torch.float32,
                                device=self.device)  # reward sum of previous step
        for i in range(buf_len - 1, -1, -1):
            buf_r_ret[i] = buf_reward[i] + buf_mask[i] * pre_r_ret
            pre_r_ret = buf_r_ret[i]
        buf_adv = buf_r_ret - (buf_mask * buf_value)
        buf_adv = (buf_adv - buf_adv.mean(dim=0)) / (buf_adv.std(dim=0) + 1e-5)
        return buf_r_ret, buf_adv

    def compute_reward_gae(self, buf_len, buf_reward, buf_mask, buf_value) -> (torch.Tensor, torch.Tensor):
        """compute the excepted discounted episode return

        :int buf_len: the length of ReplayBuffer
        :torch.Tensor buf_reward: buf_reward.shape==(buf_len, 1)
        :torch.Tensor buf_mask:   buf_mask.shape  ==(buf_len, 1)
        :torch.Tensor buf_value:  buf_value.shape ==(buf_len, 1)
        :return torch.Tensor buf_r_sum:      buf_r_sum.shape     ==(buf_len, 1)
        :return torch.Tensor buf_advantage:  buf_advantage.shape ==(buf_len, 1)
        """
        buf_r_ret = torch.empty(buf_reward.shape, dtype=torch.float32, device=self.device)  # old policy value
        buf_adv = torch.empty(buf_reward.shape, dtype=torch.float32, device=self.device)  # advantage value

        pre_r_ret = torch.zeros(buf_reward.shape[1], dtype=torch.float32,
                                device=self.device)  # reward sum of previous step
        pre_adv = torch.zeros(buf_reward.shape[1], dtype=torch.float32,
                              device=self.device)  # advantage value of previous step
        for i in range(buf_len - 1, -1, -1):
            buf_r_ret[i] = buf_reward[i] + buf_mask[i] * pre_r_ret
            pre_r_ret = buf_r_ret[i]

            buf_adv[i] = buf_reward[i] + buf_mask[i] * pre_adv - buf_value[i]
            pre_adv = buf_value[i] + buf_adv[i] * self.lambda_gae_adv

        buf_adv = (buf_adv - buf_adv.mean(dim=0)) / (buf_adv.std(dim=0) + 1e-5)
        return buf_r_ret, buf_adv


class AgentMPO(AgentBase):
    def __init__(self):
        super().__init__()
        self.epsilon_dual = 0.1
        self.epsilon_kl_mu = 0.01
        self.epsilon_kl_sigma = 0.01
        self.epsilon_kl = 0.01
        self.alpha = 10
        self.sample_a_num = 64
        self.lagrange_iteration_num = 5

    def init(self, net_dim, state_dim, action_dim, if_per):
        self.action_dim = action_dim
        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

        self.act = ActorMPO(net_dim, state_dim, action_dim).to(self.device)
        self.act_target = deepcopy(self.act)
        self.cri = Critic(net_dim, state_dim, action_dim).to(self.device)
        self.cri_target = deepcopy(self.cri)

        self.criterion = torch.nn.SmoothL1Loss()
        self.act_optimizer = torch.optim.Adam(self.act.parameters(), lr=self.learning_rate)
        self.cri_optimizer = torch.optim.Adam(self.cri.parameters(), lr=self.learning_rate)

        self.eta = np.random.rand()
        self.eta_kl_mu = 0.
        self.eta_kl_sigma = 0.
        self.eta_kl_mu = 0.

    def select_action(self, state) -> np.ndarray:
        states = torch.as_tensor((state,), dtype=torch.float32, device=self.device).detach_()
        action = self.act.get_action(states)[0]
        return action.cpu().numpy()

    def update_net(self, buffer, target_step, batch_size, repeat_times) -> (float, float):
        buffer.update_now_len_before_sample()
        t_a = torch.empty([256, 64, 3], dtype=torch.float32, device=self.device)
        t_s = torch.empty([256, 64, 15], dtype=torch.float32, device=self.device)
        obj_critic = None
        for _ in range(int(target_step * repeat_times)):
            # Policy Evaluation
            with torch.no_grad():
                reward, mask, a, state, next_s = buffer.sample_batch(batch_size)
                pi_next_s = self.act_target.get_distribution(next_s)
                sampled_next_a = pi_next_s.sample((self.sample_a_num,)).transpose(0, 1)
                expanded_next_s = next_s[:, None, :].expand(-1, self.sample_a_num, -1)
                expected_next_q = self.cri_target(
                    expanded_next_s.reshape(-1, state.shape[1]),
                    sampled_next_a.reshape(-1, a.shape[1])
                )
                expected_next_q = expected_next_q.reshape(batch_size, self.sample_a_num)
                expected_next_q = expected_next_q.mean(dim=1)
                expected_next_q = expected_next_q.unsqueeze(dim=1)
                q_label = reward + mask * expected_next_q
            q = self.cri(state, a)
            obj_critic = self.criterion(q, q_label)
            self.cri_optimizer.zero_grad()
            obj_critic.backward()
            self.cri_optimizer.step()
            self.soft_update(self.cri_target, self.cri, self.soft_update_tau)

            # Policy Improvation
            # Sample M additional action for each state
            with torch.no_grad():
                pi_b = self.act_target.get_distribution(state)
                sampled_a = pi_b.sample((self.sample_a_num,))
                expanded_s = state[None, ...].expand(self.sample_a_num, -1, -1)
                target_q = self.cri_target.forward(
                    expanded_s.reshape(-1, state.shape[1]),  # (M * B, ds)
                    sampled_a.reshape(-1, a.shape[1])  # (M * B, da)
                ).reshape(self.sample_a_num, batch_size)
                target_q_np = target_q.cpu().numpy()

            # E step
            def dual(eta):
                max_q = np.max(target_q_np, 0)
                return eta * self.epsilon_dual + np.mean(max_q) \
                       + eta * np.mean(np.log(np.mean(np.exp((target_q_np - max_q) / eta), axis=0)))

            bounds = [(1e-6, None)]
            res = minimize(dual, np.array([self.eta]), method='SLSQP', bounds=bounds)
            self.eta = res.x[0]

            qij = torch.softmax(target_q / self.eta, dim=0)  # (M, B) or (da, B)

            # M step
            pi = self.act.get_distribution(state)
            loss_p = torch.mean(
                qij * (
                        pi.expand((self.sample_a_num, batch_size)).log_prob(sampled_a)  # (M, B)
                        + pi_b.expand((self.sample_a_num, batch_size)).log_prob(sampled_a)  # (M, B)
                )
            )
            # mean_loss_p.append((-loss_p).item())
            kl_mu, kl_sigma = gaussian_kl(mu_i=pi_b.loc, mu=pi.loc, A_i=pi_b.scale_tril, A=pi.scale_tril)
            if np.isnan(kl_mu.item()):
                print('kl_μ is nan')
                embed()
            if np.isnan(kl_sigma.item()):
                print('kl_Σ is nan')
                embed()

            self.eta_kl_mu -= self.alpha * (self.epsilon_kl_mu - kl_mu).detach().item()
            self.eta_kl_sigma -= self.alpha * (self.epsilon_kl_sigma - kl_sigma).detach().item()
            self.eta_kl_mu = 0.0 if self.eta_kl_mu < 0.0 else self.eta_kl_mu
            self.eta_kl_sigma = 0.0 if self.eta_kl_sigma < 0.0 else self.eta_kl_sigma
            self.act_optimizer.zero_grad()
            obj_actor = -(
                    loss_p
                    + self.eta_kl_mu * (self.epsilon_kl_mu - kl_mu)
                    + self.eta_kl_sigma * (self.epsilon_kl_sigma - kl_sigma)
            )
            self.act_optimizer.zero_grad()
            obj_actor.backward()
            self.act_optimizer.step()
            self.soft_update(self.act_target, self.act, self.soft_update_tau)

        self.update_record(obj_a=obj_actor.item(),
                           obj_c=obj_critic.item(),
                           loss_pi=loss_p.item(),
                           est_q=q_label.mean().item(),
                           max_kl_mu=kl_mu.item(),
                           max_kl_sigma=kl_sigma.item(),
                           eta=self.eta)
        return self.train_record


'''Utils'''


def bt(m):
    return m.transpose(dim0=-2, dim1=-1)


def btr(m):
    return m.diagonal(dim1=-2, dim2=-1).sum(-1)


def gaussian_kl(mu_i, mu, A_i, A):
    """
    decoupled KL between two multivariate gaussian distribution
    C_μ = KL(f(x|μi,Σi)||f(x|μ,Σi))
    C_Σ = KL(f(x|μi,Σi)||f(x|μi,Σ))
    :param μi: (B, n)
    :param μ: (B, n)
    :param Ai: (B, n, n)
    :param A: (B, n, n)
    :return: C_μ, C_Σ: mean and covariance terms of the KL
    """
    n = A.size(-1)
    mu_i = mu_i.unsqueeze(-1)  # (B, n, 1)
    mu = mu.unsqueeze(-1)  # (B, n, 1)
    sigma_i = A_i @ bt(A_i)  # (B, n, n)
    sigma = A @ bt(A)  # (B, n, n)
    sigma_i_inv = sigma_i.inverse()  # (B, n, n)
    sigma_inv = sigma.inverse()  # (B, n, n)
    inner_mu = ((mu - mu_i).transpose(-2, -1) @ sigma_i_inv @ (mu - mu_i)).squeeze()  # (B,)
    inner_sigma = torch.log(sigma_inv.det() / sigma_i_inv.det()) - n + btr(sigma_i_inv @ sigma_inv)  # (B,)
    C_mu = 0.5 * torch.mean(inner_mu)
    C_sigma = 0.5 * torch.mean(inner_sigma)
    return C_mu, C_sigma


class OrnsteinUhlenbeckNoise:
    def __init__(self, size, theta=0.15, sigma=0.3, ou_noise=0.0, dt=1e-2):
        """The noise of Ornstein-Uhlenbeck Process

        Source: https://github.com/slowbull/DDPG/blob/master/src/explorationnoise.py
        It makes Zero-mean Gaussian Noise more stable.
        It helps agent explore better in a inertial system.
        Don't abuse OU Process. OU process has too much hyper-parameters and over fine-tuning make no sense.

        :int size: the size of noise, noise.shape==(-1, action_dim)
        :float theta: related to the not independent of OU-noise
        :float sigma: related to action noise std
        :float ou_noise: initialize OU-noise
        :float dt: derivative
        """
        self.theta = theta
        self.sigma = sigma
        self.ou_noise = ou_noise
        self.dt = dt
        self.size = size

    def __call__(self) -> float:
        """output a OU-noise

        :return array ou_noise: a noise generated by Ornstein-Uhlenbeck Process
        """
        noise = self.sigma * np.sqrt(self.dt) * rd.normal(size=self.size)
        self.ou_noise -= self.theta * self.ou_noise * self.dt + noise
        return self.ou_noise
