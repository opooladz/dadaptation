"""Created 2023
Pytorch functions for D-Adaption for Adahessian
@author: OMEAD POOLADZANDI, omeadbpooladzandi@gmail.com
Base D-Adaption code @author: adefazio
Base Hutchinson Treace code @author: amirgholami
"""

import math
from typing import TYPE_CHECKING, Any, Callable, Optional

import torch
import torch.optim
import pdb
import logging
import os
import torch.distributed as dist

if TYPE_CHECKING:
    from torch.optim.optimizer import _params_t
else:
    _params_t = Any

class DAdaptAdahessian(torch.optim.Optimizer):
    r"""
    Implements Adahessian with D-Adaptation automatic step-sizes.
    Leave LR set to 1 unless you encounter instability.

    Arguments:
        params (iterable):
            Iterable of parameters to optimize or dicts defining parameter groups.
        lr (float):
            Learning rate adjustment parameter. Increases or decreases the D-adapted learning rate.
        betas (Tuple[float, float], optional): coefficients used for computing
            running averages of gradient and its square (default: (0.9, 0.999))
        eps (float):
            Term added to the denominator outside of the root operation to improve numerical stability. (default: 1e-4).
            Adahessian defaults of 1e-4, AdaBelief can be between 1e-12 and 1e-8. 
        weight_decay (float):
            Weight decay, i.e. a L2 penalty (default: 0).
        log_every (int):
            Log using print every k steps, default 0 (no logging).
        decouple (boolean):
            Use AdamW style decoupled weight decay
        use_bias_correction (boolean):
            Turn on Adam's bias correction. Off by default.
        d0 (float):
            Initial D estimate for D-adaptation (default 1e-6). Rarely needs changing.
        growth_rate (float):
            prevent the D estimate from growing faster than this multiplicative rate.
            Default is inf, for unrestricted. Values like 1.02 give a kind of learning
            rate warmup effect.
        fsdp_in_use (bool):
            If you're using sharded parameters, this should be set to True. The optimizer
            will attempt to auto-detect this, but if you're using an implementation other
            than PyTorch's builtin version, the auto-detection won't work.
        belief (bool): 
            Consider using belief system. There is no official paper on this but helps with the XOR problem
    """
    def __init__(self, params, lr=1.0,
                 betas=(0.9, 0.999), eps=1e-4,
                 weight_decay=0, log_every=0,
                 decouple=False,
                 use_bias_correction=False,
                 d0=1e-6, growth_rate=float('inf'),
                 fsdp_in_use=False, belief=False):
        if not 0.0 < d0:
            raise ValueError("Invalid d0 value: {}".format(d0))
        if not 0.0 < lr:
            raise ValueError("Invalid learning rate: {}".format(lr))
        if not 0.0 < eps:
            raise ValueError("Invalid epsilon value: {}".format(eps))
        if not 0.0 <= betas[0] < 1.0:
            raise ValueError("Invalid beta parameter at index 0: {}".format(betas[0]))
        if not 0.0 <= betas[1] < 1.0:
            raise ValueError("Invalid beta parameter at index 1: {}".format(betas[1]))

        if decouple:
            print(f"Using decoupled weight decay")


        defaults = dict(lr=lr, betas=betas, eps=eps,
                        weight_decay=weight_decay,
                        d = d0,
                        belief=belief,
                        k=0,
                        numerator_weighted=0.0,
                        log_every=log_every,
                        growth_rate=growth_rate,
                        use_bias_correction=use_bias_correction,
                        decouple=decouple,
                        fsdp_in_use=fsdp_in_use)
        self.belief = belief
        self.d0 = d0
        super().__init__(params, defaults)

    @property
    def supports_memory_efficient_fp16(self):
        return False

    @property
    def supports_flat_params(self):
        return True
    def get_trace(self, params, grads):
        """
        compute the Hessian vector product with a random vector v, at the current gradient point,
        i.e., compute the gradient of <gradsH,v>.
        :param gradsH: a list of torch variables
        :return: a list of torch tensors
        """

        # Check backward was called with create_graph set to True
        for i, grad in enumerate(grads):
            if grad.grad_fn is None:
                raise RuntimeError('Gradient tensor {:} does not have grad_fn. When calling\n'.format(i) +
                           '\t\t\t  loss.backward(), make sure the option create_graph is\n' +
                           '\t\t\t  set to True.')

        v = [2 * torch.randint_like(p, high=2) - 1 for p in params]



        hvs = torch.autograd.grad(
            grads,
            params,
            grad_outputs=v,
            only_inputs=True,
            retain_graph=True)

        hutchinson_trace = []
        for hv in hvs:
            param_size = hv.size()
            if len(param_size) <= 2:  # for 0/1/2D tensor
                # Hessian diagonal block size is 1 here.
                # We use that torch.abs(hv * vi) = hv.abs()
                tmp_output = hv.abs()

            elif len(param_size) == 4:  # Conv kernel
                # Hessian diagonal block size is 9 here: torch.sum() reduces the dim 2/3.
                # We use that torch.abs(hv * vi) = hv.abs()
                tmp_output = torch.mean(hv.abs(), dim=[2, 3], keepdim=True)
            hutchinson_trace.append(tmp_output)


        return hutchinson_trace
    def step(self, closure=None):
        """Performs a single optimization step.

        Arguments:
            closure (callable, optional): A closure that reevaluates the model
                and returns the loss.
        """
        loss = None
        if closure is not None:
            loss = closure()

        sk_l1 = 0.0

        group = self.param_groups[0]
        use_bias_correction = group['use_bias_correction']
        numerator_weighted = group['numerator_weighted']
        beta1, beta2 = group['betas']
        k = group['k']

        d = group['d']
        lr = max(group['lr'] for group in self.param_groups)

        if use_bias_correction:
            bias_correction = ((1-beta2**(k+1))**0.5)/(1-beta1**(k+1))
        else:
            bias_correction = 1

        dlr = d*lr*bias_correction

        growth_rate = group['growth_rate']
        decouple = group['decouple']
        log_every = group['log_every']


        sqrt_beta2 = beta2**(0.5)

        numerator_acum = 0.0


        params = []
        groups = []
        grads = []

        # Flatten groups into lists, so that
        #  hut_traces can be called with lists of parameters
        #  and grads
        for group in self.param_groups:
            for p in group['params']:
                if p.grad is not None:
                    params.append(p)
                    groups.append(group)
                    grads.append(p.grad)

        # get the Hessian diagonal

        hut_traces = self.get_trace(params, grads)

        for (p, group, grad, hut_trace) in zip(params, groups, grads, hut_traces):

            decay = group['weight_decay']
            k = group['k']
            eps = group['eps']
            group_lr = group['lr']

            if group_lr not in [lr, 0.0]:
                raise RuntimeError(f"Setting different lr values in different parameter groups is only supported for values of 0")

            for p in group['params']:
                if p.grad is None:
                    continue

                grad = p.grad.data

                # Apply weight decay (coupled variant)
                if decay != 0 and not decouple:
                    grad.add_(p.data, alpha=decay)

            state = self.state[p]

            # State initialization

            # if 'step' not in state:
            if len(state) == 0:
                state['step'] = 0
                state['s'] = torch.zeros_like(p.data).detach()
                # Exponential moving average of gradient values
                state['exp_avg'] = torch.zeros_like(p.data).detach()
                # Exponential moving average of squared gradient values
                state['exp_avg_sq'] = torch.zeros_like(p.data).detach()

            exp_avg, exp_avg_sq = state['exp_avg'], state['exp_avg_sq']

            s = state['s']

            if group_lr > 0.0:
                denom = exp_avg_sq.sqrt().add_(eps)
                numerator_acum += dlr * torch.dot(grad.flatten(), s.div(denom).flatten()).item()

                # Adam EMA updates
                exp_avg.mul_(beta1).add_(grad.detach_(), alpha=dlr*(1-beta1))
                if self.belief:
                  exp_avg_sq.mul_(beta2).addcmul_(hut_trace-exp_avg, hut_trace-exp_avg, value=1-beta2)
                else:
                  exp_avg_sq.mul_(beta2).addcmul_(hut_trace, hut_trace, value=1-beta2)
                s.mul_(sqrt_beta2).add_(grad.detach_(), alpha=dlr*(1-sqrt_beta2))
                sk_l1 += s.abs().sum().item()

        ######

        numerator_weighted = sqrt_beta2*numerator_weighted + (1-sqrt_beta2)*numerator_acum
        d_hat = d

        # if we have not done any progres, return
        # if we have any gradients available, will have sk_l1 > 0 (unless \|g\|=0)
        if sk_l1 == 0:
            return loss

        if lr > 0.0:

            global_numerator_weighted = numerator_weighted
            global_sk_l1 = sk_l1


            d_hat = global_numerator_weighted/((1-sqrt_beta2)*global_sk_l1)
            d = max(d, min(d_hat, d*growth_rate))

        if log_every > 0 and k % log_every == 0:
            logging.info(f"lr: {lr} dlr: {dlr} d_hat: {d_hat}, d: {d}. sk_l1={global_sk_l1:1.1e} numerator_weighted={global_numerator_weighted:1.1e}")

        for group in self.param_groups:
            group['numerator_weighted'] = numerator_weighted
            group['d'] = d

            decay = group['weight_decay']
            k = group['k']
            eps = group['eps']

            for p in group['params']:
                if p.grad is None:
                    continue
                grad = p.grad.data

                state = self.state[p]

                exp_avg, exp_avg_sq = state['exp_avg'], state['exp_avg_sq']

                state['step'] += 1

                denom = exp_avg_sq.sqrt().add_(eps)

                # Apply weight decay (decoupled variant)
                if decay != 0 and decouple:
                    p.data.add_(p.data, alpha=-decay * dlr)


                ### Take step
                p.data.addcdiv_(exp_avg, denom, value=-1)

            group['k'] = k + 1

        return loss
