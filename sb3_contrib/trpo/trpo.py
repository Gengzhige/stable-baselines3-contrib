import copy
import warnings
from functools import partial
from typing import Any, ClassVar, Optional, TypeVar, Union

import numpy as np
import torch as th
from gymnasium import spaces
from stable_baselines3.common.buffers import RolloutBuffer
from stable_baselines3.common.distributions import kl_divergence
from stable_baselines3.common.on_policy_algorithm import OnPolicyAlgorithm
from stable_baselines3.common.policies import ActorCriticPolicy, BasePolicy
from stable_baselines3.common.type_aliases import GymEnv, MaybeCallback, RolloutBufferSamples, Schedule
from stable_baselines3.common.utils import explained_variance
from torch import nn
from torch.nn import functional as F

from sb3_contrib.common.utils import conjugate_gradient_solver, flat_grad
from sb3_contrib.trpo.policies import CnnPolicy, MlpPolicy, MultiInputPolicy

SelfTRPO = TypeVar("SelfTRPO", bound="TRPO")


class TRPO(OnPolicyAlgorithm):
    """
    Trust Region Policy Optimization (TRPO)

    Paper: https://arxiv.org/abs/1502.05477
    Code: This implementation borrows code from OpenAI Spinning Up (https://github.com/openai/spinningup/)
    and Stable Baselines (TRPO from https://github.com/hill-a/stable-baselines)

    Introduction to TRPO: https://spinningup.openai.com/en/latest/algorithms/trpo.html

    :param policy: The policy model to use (MlpPolicy, CnnPolicy, ...)
    :param env: The environment to learn from (if registered in Gym, can be str)
    :param learning_rate: The learning rate for the value function, it can be a function
        of the current progress remaining (from 1 to 0)
    :param n_steps: The number of steps to run for each environment per update
        (i.e. rollout buffer size is n_steps * n_envs where n_envs is number of environment copies running in parallel)
        NOTE: n_steps * n_envs must be greater than 1 (because of the advantage normalization)
        See https://github.com/pytorch/pytorch/issues/29372
    :param batch_size: Minibatch size for the value function
    :param gamma: Discount factor
    :param cg_max_steps: maximum number of steps in the Conjugate Gradient algorithm
        for computing the Hessian vector product
    :param cg_damping: damping in the Hessian vector product computation
    :param line_search_shrinking_factor: step-size reduction factor for the line-search
        (i.e., ``theta_new = theta + alpha^i * step``)
    :param line_search_max_iter: maximum number of iteration
        for the backtracking line-search
    :param n_critic_updates: number of critic updates per policy update
    :param gae_lambda: Factor for trade-off of bias vs variance for Generalized Advantage Estimator
    :param use_sde: Whether to use generalized State Dependent Exploration (gSDE)
        instead of action noise exploration (default: False)
    :param sde_sample_freq: Sample a new noise matrix every n steps when using gSDE
        Default: -1 (only sample at the beginning of the rollout)
    :param rollout_buffer_class: Rollout buffer class to use. If ``None``, it will be automatically selected.
    :param rollout_buffer_kwargs: Keyword arguments to pass to the rollout buffer on creation
    :param normalize_advantage: Whether to normalize or not the advantage
    :param target_kl: Target Kullback-Leibler divergence between updates.
        Should be small for stability. Values like 0.01, 0.05.
    :param sub_sampling_factor: Sub-sample the batch to make computation faster
        see p40-42 of John Schulman thesis http://joschu.net/docs/thesis.pdf
    :param stats_window_size: Window size for the rollout logging, specifying the number of episodes to average
        the reported success rate, mean episode length, and mean reward over
    :param tensorboard_log: the log location for tensorboard (if None, no logging)
    :param policy_kwargs: additional arguments to be passed to the policy on creation. See :ref:`trpo_policies`
    :param verbose: the verbosity level: 0 no output, 1 info, 2 debug
    :param seed: Seed for the pseudo random generators
    :param device: Device (cpu, cuda, ...) on which the code should be run.
        Setting it to auto, the code will be run on the GPU if possible.
    :param _init_setup_model: Whether or not to build the network at the creation of the instance
    """

    policy_aliases: ClassVar[dict[str, type[BasePolicy]]] = {
        "MlpPolicy": MlpPolicy,
        "CnnPolicy": CnnPolicy,
        "MultiInputPolicy": MultiInputPolicy,
    }

    def __init__(
        self,
        policy: Union[str, type[ActorCriticPolicy]],
        env: Union[GymEnv, str],
        learning_rate: Union[float, Schedule] = 1e-3,
        n_steps: int = 2048,
        batch_size: int = 128,
        gamma: float = 0.99,
        cg_max_steps: int = 15,
        cg_damping: float = 0.1,
        line_search_shrinking_factor: float = 0.8,
        line_search_max_iter: int = 10,
        n_critic_updates: int = 10,
        gae_lambda: float = 0.95,
        use_sde: bool = False,
        sde_sample_freq: int = -1,
        rollout_buffer_class: Optional[type[RolloutBuffer]] = None,
        rollout_buffer_kwargs: Optional[dict[str, Any]] = None,
        normalize_advantage: bool = True,
        target_kl: float = 0.01,
        sub_sampling_factor: int = 1,
        stats_window_size: int = 100,
        tensorboard_log: Optional[str] = None,
        policy_kwargs: Optional[dict[str, Any]] = None,
        verbose: int = 0,
        seed: Optional[int] = None,
        device: Union[th.device, str] = "auto",
        _init_setup_model: bool = True,
    ):
        super().__init__(
            policy,
            env,
            learning_rate=learning_rate,
            n_steps=n_steps,
            gamma=gamma,
            gae_lambda=gae_lambda,
            ent_coef=0.0,  # entropy bonus is not used by TRPO
            vf_coef=0.0,  # value function is optimized separately
            max_grad_norm=0.0,
            use_sde=use_sde,
            sde_sample_freq=sde_sample_freq,
            rollout_buffer_class=rollout_buffer_class,
            rollout_buffer_kwargs=rollout_buffer_kwargs,
            stats_window_size=stats_window_size,
            tensorboard_log=tensorboard_log,
            policy_kwargs=policy_kwargs,
            verbose=verbose,
            device=device,
            seed=seed,
            _init_setup_model=False,
            supported_action_spaces=(
                spaces.Box,
                spaces.Discrete,
                spaces.MultiDiscrete,
                spaces.MultiBinary,
            ),
        )

        self.normalize_advantage = normalize_advantage
        # Sanity check, otherwise it will lead to noisy gradient and NaN
        # because of the advantage normalization
        if self.env is not None:
            # Check that `n_steps * n_envs > 1` to avoid NaN
            # when doing advantage normalization
            buffer_size = self.env.num_envs * self.n_steps
            if normalize_advantage:
                assert buffer_size > 1, (
                    "`n_steps * n_envs` must be greater than 1. "
                    f"Currently n_steps={self.n_steps} and n_envs={self.env.num_envs}"
                )
            # Check that the rollout buffer size is a multiple of the mini-batch size
            untruncated_batches = buffer_size // batch_size
            if buffer_size % batch_size > 0:
                warnings.warn(
                    f"You have specified a mini-batch size of {batch_size},"
                    f" but because the `RolloutBuffer` is of size `n_steps * n_envs = {buffer_size}`,"
                    f" after every {untruncated_batches} untruncated mini-batches,"
                    f" there will be a truncated mini-batch of size {buffer_size % batch_size}\n"
                    f"We recommend using a `batch_size` that is a factor of `n_steps * n_envs`.\n"
                    f"Info: (n_steps={self.n_steps} and n_envs={self.env.num_envs})"
                )
        self.batch_size = batch_size
        # Conjugate gradients parameters
        self.cg_max_steps = cg_max_steps
        self.cg_damping = cg_damping
        # Backtracking line search parameters
        self.line_search_shrinking_factor = line_search_shrinking_factor
        self.line_search_max_iter = line_search_max_iter
        self.target_kl = target_kl
        self.n_critic_updates = n_critic_updates
        self.sub_sampling_factor = sub_sampling_factor

        if _init_setup_model:
            self._setup_model()

    def _compute_actor_grad(
        self, kl_div: th.Tensor, policy_objective: th.Tensor
    ) -> tuple[list[nn.Parameter], th.Tensor, th.Tensor, list[tuple[int, ...]]]:
        """
        Compute actor gradients for kl div and surrogate objectives.

        :param kl_div: The KL divergence objective
        :param policy_objective: The surrogate objective ("classic" policy gradient)
        :return: List of actor params, gradients and gradients shape.
        """
        # This is necessary because not all the parameters in the policy have gradients w.r.t. the KL divergence
        # The policy objective is also called surrogate objective
        policy_objective_gradients_list = []
        # Contains the gradients of the KL divergence
        grad_kl_list = []
        # Contains the shape of the gradients of the KL divergence w.r.t each parameter
        # This way the flattened gradient can be reshaped back into the original shapes and applied to
        # the parameters
        grad_shape: list[tuple[int, ...]] = []
        # Contains the parameters which have non-zeros KL divergence gradients
        # The list is used during the line-search to apply the step to each parameters
        actor_params: list[nn.Parameter] = []

        # 遍历策略网络中的所有参数
        for name, param in self.policy.named_parameters():
            # Skip parameters related to value function based on name
            # this work for built-in policies only (not custom ones)
            # 跳过价值函数的部分
            if "value" in name:
                continue

            # For each parameter we compute the gradient of the KL divergence w.r.t to that parameter
            # 借助pytorch框架计算参数对KL散度的梯度
            kl_param_grad, *_ = th.autograd.grad(
                kl_div,
                param,
                create_graph=True,
                retain_graph=True,
                allow_unused=True,
                only_inputs=True,
            )
            # If the gradient is not zero (not None), we store the parameter in the actor_params list
            # and add the gradient and its shape to grad_kl and grad_shape respectively
            # 梯度不为None，也就是说该参数确实影响KL散度
            if kl_param_grad is not None:
                # If the parameter impacts the KL divergence (i.e. the policy)
                # we compute the gradient of the policy objective w.r.t to the parameter
                # this avoids computing the gradient if it's not going to be used in the conjugate gradient step
                # 计算参数对策略目标的梯度
                policy_objective_grad, *_ = th.autograd.grad(policy_objective, param, retain_graph=True, only_inputs=True)

                # 记录梯度形状、KL散度梯度、策略目标梯度、参数
                grad_shape.append(kl_param_grad.shape)
                grad_kl_list.append(kl_param_grad.reshape(-1))
                policy_objective_gradients_list.append(policy_objective_grad.reshape(-1))
                actor_params.append(param)

        # Gradients are concatenated before the conjugate gradient step
        # 拼接后返回
        policy_objective_gradients = th.cat(policy_objective_gradients_list)
        grad_kl = th.cat(grad_kl_list)
        return actor_params, policy_objective_gradients, grad_kl, grad_shape

    def train(self) -> None:
        """
        Update policy using the currently gathered rollout buffer.
        实现主要参考了OpenAI Spinning Up的代码
        https://spinningup.openai.com/en/latest/algorithms/trpo.html
        """
        # Switch to train mode (this affects batch norm / dropout)
        self.policy.set_training_mode(True)
        # Update optimizer learning rate
        self._update_learning_rate(self.policy.optimizer)

        policy_objective_values = []
        kl_divergences = []
        line_search_results = []
        value_losses = []

        # This will only loop once (get all data in one go)
        # batch_size设为None其实就是返回所有数据的意思
        for rollout_data in self.rollout_buffer.get(batch_size=None):
            # Optional: sub-sample data for faster computation
            # 进一步采样，跳过
            if self.sub_sampling_factor > 1:
                rollout_data = RolloutBufferSamples(
                    rollout_data.observations[:: self.sub_sampling_factor],
                    rollout_data.actions[:: self.sub_sampling_factor],
                    None,  # type: ignore[arg-type]  # old values, not used here
                    rollout_data.old_log_prob[:: self.sub_sampling_factor],
                    rollout_data.advantages[:: self.sub_sampling_factor],
                    None,  # type: ignore[arg-type]  # returns, not used here
                )

            actions = rollout_data.actions
            if isinstance(self.action_space, spaces.Discrete):
                # Convert discrete action from float to long
                # 离散action数据类型转换
                actions = rollout_data.actions.long().flatten()

            with th.no_grad():
                # Note: is copy enough, no need for deepcopy?
                # If using gSDE and deepcopy, we need to use `old_distribution.distribution`
                # directly to avoid PyTorch errors.
                # 获取旧策略在给定观测状态下的动作概率分布
                old_distribution = copy.copy(self.policy.get_distribution(rollout_data.observations))

            # 当前策略在给定观测状态下的动作概率分布，并计算动作的对数概率
            distribution = self.policy.get_distribution(rollout_data.observations)
            log_prob = distribution.log_prob(actions)

            # 获取优势，并进行标准化处理
            # 这里的优势在buffer中已经计算好了，采用的方法是GAE
            advantages = rollout_data.advantages
            if self.normalize_advantage:
                advantages = (advantages - advantages.mean()) / (rollout_data.advantages.std() + 1e-8)

            # ratio between old and new policy, should be one at the first iteration
            # 策略比值，e的差次幂
            ratio = th.exp(log_prob - rollout_data.old_log_prob)

            # surrogate policy objective
            # 比值乘以优势的部分，策略目标
            policy_objective = (advantages * ratio).mean()

            # KL divergence
            # 新策略和旧策略之间的KL散度
            kl_div = kl_divergence(distribution, old_distribution).mean()

            # Surrogate & KL gradient
            self.policy.optimizer.zero_grad()

            # 计算KL散度梯度和策略目标梯度
            actor_params, policy_objective_gradients, grad_kl, grad_shape = self._compute_actor_grad(kl_div, policy_objective)

            # Hessian-vector dot product function used in the conjugate gradient step
            # 创建一个函数，用于共轭梯度求解器中，近似求解Fisher信息矩阵与向量的乘积
            hessian_vector_product_fn = partial(self.hessian_vector_product, actor_params, grad_kl)

            # Computing search direction
            # 共轭梯度近似求解最优更新方向，这部分公式的推导可见论文附录C
            # 也就是Fisher信息矩阵的逆矩阵H^{-1}与策略目标梯度g的乘积
            # 由于逆矩阵计算量较大，采用共轭梯度近似求解
            search_direction = conjugate_gradient_solver(
                hessian_vector_product_fn,
                policy_objective_gradients,
                max_iter=self.cg_max_steps,
            )

            # Maximal step length
            # 计算最大步长
            # 2倍目标KL散度值，常数超参数
            line_search_max_step_size = 2 * self.target_kl
            # 分母部分，故意兜了个圈子，有兴趣的话可以尝试推导一下，(H^{-1}g)^T H H^{-1}g，也可以想想有没有更简单的写法
            line_search_max_step_size /= th.matmul(
                search_direction, hessian_vector_product_fn(search_direction, retain_graph=False)
            )
            # 开方得到线搜索步长
            line_search_max_step_size = th.sqrt(line_search_max_step_size)  # type: ignore[assignment, arg-type]

            # 线搜索步长缩减系数
            line_search_backtrack_coeff = 1.0
            # 保存原始参数，用于回溯
            original_actor_params = [param.detach().clone() for param in actor_params]

            # flag
            is_line_search_success = False
            with th.no_grad():
                # Line-search (backtracking)
                for _ in range(self.line_search_max_iter): # 默认是10
                    start_idx = 0
                    # Applying the scaled step direction
                    # 纯手动更新每一个参数
                    for param, original_param, shape in zip(actor_params, original_actor_params, grad_shape):
                        n_params = param.numel()
                        # 与公式完全对应，更新参数
                        param.data = (
                            original_param.data
                            + line_search_backtrack_coeff
                            * line_search_max_step_size
                            * search_direction[start_idx : (start_idx + n_params)].view(shape)
                        )
                        start_idx += n_params

                    # Recomputing the policy log-probabilities
                    # 重新计算给定观测状态下的动作概率分布和对数动作概率
                    distribution = self.policy.get_distribution(rollout_data.observations)
                    log_prob = distribution.log_prob(actions)

                    # New policy objective
                    # 策略比值，策略目标
                    ratio = th.exp(log_prob - rollout_data.old_log_prob)
                    new_policy_objective = (advantages * ratio).mean()

                    # New KL-divergence
                    # KL散度
                    kl_div = kl_divergence(distribution, old_distribution).mean()

                    # Constraint criteria:
                    # we need to improve the surrogate policy objective
                    # while being close enough (in term of kl div) to the old policy
                    # KL散度不能差太多，且策略目标确实有提升
                    if (kl_div < self.target_kl) and (new_policy_objective > policy_objective):
                        is_line_search_success = True
                        break

                    # Reducing step size if line-search wasn't successful
                    # 步长缩减因子幂次递减，同样映射公式里的因子
                    line_search_backtrack_coeff *= self.line_search_shrinking_factor

                line_search_results.append(is_line_search_success)

                if not is_line_search_success:
                    # If the line-search wasn't successful we revert to the original parameters
                    # 先搜索不成功则回溯参数
                    for param, original_param in zip(actor_params, original_actor_params):
                        param.data = original_param.data.clone()

                    policy_objective_values.append(policy_objective.item())
                    kl_divergences.append(0.0)
                else: # 记录数值作为log输出
                    policy_objective_values.append(new_policy_objective.item())
                    kl_divergences.append(kl_div.item())

        # Critic update
        # 价值函数更新，就是普通的MSE
        for _ in range(self.n_critic_updates):
            for rollout_data in self.rollout_buffer.get(self.batch_size):
                values_pred = self.policy.predict_values(rollout_data.observations)
                value_loss = F.mse_loss(rollout_data.returns, values_pred.flatten())
                value_losses.append(value_loss.item())

                self.policy.optimizer.zero_grad()
                value_loss.backward()
                # Removing gradients of parameters shared with the actor
                # otherwise it defeats the purposes of the KL constraint
                for param in actor_params:
                    param.grad = None
                self.policy.optimizer.step()

        self._n_updates += 1
        explained_var = explained_variance(self.rollout_buffer.values.flatten(), self.rollout_buffer.returns.flatten())

        # Logs
        self.logger.record("train/policy_objective", np.mean(policy_objective_values))
        self.logger.record("train/value_loss", np.mean(value_losses))
        self.logger.record("train/kl_divergence_loss", np.mean(kl_divergences))
        self.logger.record("train/explained_variance", explained_var)
        self.logger.record("train/is_line_search_success", np.mean(line_search_results))
        if hasattr(self.policy, "log_std"):
            self.logger.record("train/std", th.exp(self.policy.log_std).mean().item())

        self.logger.record("train/n_updates", self._n_updates, exclude="tensorboard")

    def hessian_vector_product(
        self, params: list[nn.Parameter], grad_kl: th.Tensor, vector: th.Tensor, retain_graph: bool = True
    ) -> th.Tensor:
        """
        Computes the matrix-vector product with the Fisher information matrix.

        :param params: list of parameters used to compute the Hessian
        :param grad_kl: flattened gradient of the KL divergence between the old and new policy
        :param vector: vector to compute the dot product the hessian-vector dot product with
        :param retain_graph: if True, the graph will be kept after computing the Hessian
        :return: Hessian-vector dot product (with damping)
        """
        jacobian_vector_product = (grad_kl * vector).sum()
        return flat_grad(jacobian_vector_product, params, retain_graph=retain_graph) + self.cg_damping * vector

    def learn(
        self: SelfTRPO,
        total_timesteps: int,
        callback: MaybeCallback = None,
        log_interval: int = 1,
        tb_log_name: str = "TRPO",
        reset_num_timesteps: bool = True,
        progress_bar: bool = False,
    ) -> SelfTRPO:
        return super().learn(
            total_timesteps=total_timesteps,
            callback=callback,
            log_interval=log_interval,
            tb_log_name=tb_log_name,
            reset_num_timesteps=reset_num_timesteps,
            progress_bar=progress_bar,
        )
