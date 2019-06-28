# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.
#

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals

import torch
import torch.nn as nn
import torch.optim as optim

from advertorch.utils import calc_l2distsq
from advertorch.utils import calc_l1dist
from advertorch.utils import tanh_rescale
from advertorch.utils import torch_arctanh
from advertorch.utils import clamp
from advertorch.utils import to_one_hot
from advertorch.utils import replicate_input
from advertorch.utils import polynomial_decay

from .base import Attack
from .base import LabelMixin
from .utils import is_successful


DIST_UPPER = 1e10
COEFF_UPPER = 1e10
INVALID_LABEL = -1
REPEAT_STEP = 10
ONE_MINUS_EPS = 0.999999
UPPER_CHECK = 1e9
PREV_LOSS_INIT = 1e6
TARGET_MULT = 10000
NUM_CHECKS = 10

class ElasticNetL1Attack(Attack, LabelMixin):
    """
    The ElasticNet L1 Attack, https://arxiv.org/abs/1709.04114

    :param predict: forward pass function.
    :param num_classes: number of clasess.
    :param confidence: confidence of the adversarial examples.
    :param targeted: if the attack is targeted.
    :param learning_rate: the learning rate for the attack algorithm
    :param binary_search_steps: number of binary search times to find the optimum
    :param max_iterations: the maximum number of iterations
    :param abort_early: if set to true, abort early if getting stuck in local min
    :param initial_const: initial value of the constant c
    :param clip_min: mininum value per input dimension.
    :param clip_max: maximum value per input dimension.
    :param beta: hyperparameter trading off L2 minimization for L1 minimization
    :param decision_rule: EN or L1. Select final adversarial example from
                          all successful examples based on the least
                          elastic-net or L1 distortion criterion.
    :param loss_fn: loss function
    """
    def __init__(self, predict, num_classes, confidence=0,
                 targeted=False, learning_rate=1e-2,
                 binary_search_steps=9, max_iterations=10000,
                 abort_early=False, initial_const=1e-3,
                 clip_min=0., clip_max=1., beta=1e-2, decision_rule='EN',
                 loss_fn=None):
        """ElasticNet L1 Attack implementation in pytorch."""
        if loss_fn is not None:
            import warnings
            warnings.warn(
                "This Attack currently do not support a different loss"
                " function other than the default. Setting loss_fn manually"
                " is not effective."
            )

        loss_fn = None

        super(ElasticNetL1Attack, self).__init__(
            predict, loss_fn, clip_min, clip_max)

        self.learning_rate = learning_rate
        self.max_iterations = max_iterations
        self.binary_search_steps = binary_search_steps
        self.abort_early = abort_early
        self.confidence = confidence
        self.initial_const = initial_const
        self.num_classes = num_classes
        self.beta = beta
        # The last iteration (if we run many steps) repeat the search once.
        self.repeat = binary_search_steps >= REPEAT_STEP
        self.targeted = targeted
        self.decision_rule = decision_rule


    def _loss_fn(self, output, y_onehot, l1dist, l2distsq, const):

        real = (y_onehot * output).sum(dim=1)
        other = ((1.0 - y_onehot) * output - (y_onehot * TARGET_MULT)).max(1)[0]

        if self.targeted:
            loss1 = clamp(other - real + self.confidence, min=0.)
        else:
            loss1 = clamp(real - other + self.confidence, min=0.)

        loss21 = l1dist.sum()
        loss2 = l2distsq.sum()

        loss1 = torch.sum(const * loss1)

        loss = loss1 + loss2 + (self.beta * loss21)
        return loss


    def _loss_opt_fn(self, output_y, y_onehot, l2distsq_y, const):

        real_y = (y_onehot * output_y).sum(dim=1)
        other_y = ((1.0 - y_onehot) * output_y - (y_onehot * TARGET_MULT)).max(1)[0]

        if self.targeted:
            loss1_y = clamp(other_y - real_y + self.confidence, min=0.)
        else:
            loss1_y = clamp(real_y - other_y + self.confidence, min=0.)

        loss1_y = torch.sum(const * loss1_y)
        loss2_y = l2distsq_y.sum()
        loss_opt = loss1_y + loss2_y
        return loss_opt

    def _is_successful(self, output, label, is_logits):
        # determine success, see if confidence-adjusted logits give the right
        #   label
        if is_logits:
            output = output.detach().clone()
            if self.targeted:
                output[torch.arange(len(label)).long(), label] -= self.confidence
            else:
                output[torch.arange(len(label)).long(), label] += self.confidence
            pred = torch.argmax(output, dim=1)
        else:
            pred = output
            if pred == INVALID_LABEL:
                return pred.new_zeros(pred.shape).byte()

        return is_successful(pred, label, self.targeted)


    def _fast_iterative_shrinkage_thresholding(self, x, slack, newimg):

        zt = self.global_step / (self.global_step + 3)

        upper = clamp(slack - self.beta, max=self.clip_max)
        lower = clamp(slack + self.beta, min=self.clip_min)

        diff = slack - x
        cond1 = (diff > self.beta).float()
        cond2 = (torch.abs(diff) <= self.beta).float()
        cond3 = (diff < -self.beta).float()

        assign_newimg = (cond1 * upper) + (cond2 * x) + (cond3 * lower)
        slack.data = assign_newimg + (zt * (assign_newimg - newimg))
        return slack, assign_newimg


    def train(self, optimizer, x, slack, y_onehot, loss_coeffs):
        optimizer.zero_grad()
        output_y = self.predict(slack)
        l2distsq_y = calc_l2distsq(slack, x)
        loss_opt = self._loss_opt_fn(output_y, y_onehot, l2distsq_y, loss_coeffs)
        loss_opt.backward()
        optimizer.step()
        self.global_step += 1
        return slack


    def run(self, x, newimg, y_onehot, loss_coeffs):

        output = self.predict(newimg)

        l2distsq = calc_l2distsq(newimg, x)
        l1dist = calc_l1dist(newimg, x)

        if self.decision_rule == 'EN':
          crit = l2distsq + (l1dist * self.beta)
        elif self.decision_rule == 'L1':
          crit = l1dist

        loss = self._loss_fn(output, y_onehot, l1dist, l2distsq, loss_coeffs)
        return loss.item(), crit.data, output.data


    def _update_if_smaller_dist_succeed(
            self, adv_img, labs, output, dist, batch_size,
            cur_dist, cur_labels,
            final_dist, final_labels, final_advs):

        target_label = labs
        output_logits = output
        _, output_label = torch.max(output_logits, 1)

        mask = (dist < cur_dist) & self._is_successful(
            output_logits, target_label, True)

        cur_dist[mask] = dist[mask]  # redundant
        cur_labels[mask] = output_label[mask]

        mask = (dist < final_dist) & self._is_successful(
            output_logits, target_label, True)
        final_dist[mask] = dist[mask]
        final_labels[mask] = output_label[mask]
        final_advs[mask] = adv_img[mask]


    def _update_loss_coeffs(
            self, labs, cur_labels, batch_size, loss_coeffs,
            coeff_upper_bound, coeff_lower_bound):

        # TODO: remove for loop, not significant, since only called during each
        # binary search step
        for ii in range(batch_size):
            cur_labels[ii] = int(cur_labels[ii])
            if self._is_successful(cur_labels[ii], labs[ii], False):
                coeff_upper_bound[ii] = min(
                    coeff_upper_bound[ii], loss_coeffs[ii])

                if coeff_upper_bound[ii] < UPPER_CHECK:
                    loss_coeffs[ii] = (
                        coeff_lower_bound[ii] + coeff_upper_bound[ii]) / 2
            else:
                coeff_lower_bound[ii] = max(
                    coeff_lower_bound[ii], loss_coeffs[ii])
                if coeff_upper_bound[ii] < UPPER_CHECK:
                    loss_coeffs[ii] = (
                        coeff_lower_bound[ii] + coeff_upper_bound[ii]) / 2
                else:
                    loss_coeffs[ii] *= 10


    def perturb(self, x, y=None):

        x, y = self._verify_and_process_inputs(x, y)

        # Initialization
        if y is None:
            y = self._get_predicted_label(x)

        x = replicate_input(x)
        batch_size = len(x)
        coeff_lower_bound = x.new_zeros(batch_size)
        coeff_upper_bound = x.new_ones(batch_size) * COEFF_UPPER
        loss_coeffs = torch.ones_like(y).float() * self.initial_const

        final_dist = [DIST_UPPER] * batch_size
        final_labels = [INVALID_LABEL] * batch_size

        final_advs = x.clone()
        y_onehot = to_one_hot(y, self.num_classes).float()

        final_dist = torch.FloatTensor(final_dist).to(x.device)
        final_labels = torch.LongTensor(final_labels).to(x.device)

        # Start binary search
        for outer_step in range(self.binary_search_steps):

            self.global_step = 0

            slack = nn.Parameter(x.clone())
            newimg = x.clone()

            optimizer = optim.SGD([slack], lr=self.learning_rate)

            cur_dist = [DIST_UPPER] * batch_size
            cur_labels = [INVALID_LABEL] * batch_size

            cur_dist = torch.FloatTensor(cur_dist).to(x.device)
            cur_labels = torch.LongTensor(cur_labels).to(x.device)

            prevloss = PREV_LOSS_INIT

            if (self.repeat and outer_step == (self.binary_search_steps - 1)):
                loss_coeffs = coeff_upper_bound

            for ii in range(self.max_iterations):

                self.train(optimizer, x, slack, y_onehot, loss_coeffs)

                polynomial_decay(optimizer,
                    self.learning_rate,
                    self.global_step,
                    self.max_iterations,
                    0,
                    power=0.5)

                slack, newimg  = self._fast_iterative_shrinkage_thresholding(
                  x, slack, newimg)
                loss, dist, output = self.run(
                  x, newimg, y_onehot, loss_coeffs)
                adv_img = newimg.data

                if self.abort_early:
                    if ii % (self.max_iterations // NUM_CHECKS or 1) == 0:
                        if loss > prevloss * ONE_MINUS_EPS:
                            break
                        prevloss = loss

                self._update_if_smaller_dist_succeed(
                  adv_img, y, output, dist, batch_size,
                  cur_dist, cur_labels,
                  final_dist, final_labels, final_advs)

            self._update_loss_coeffs(
                y, cur_labels, batch_size,
                loss_coeffs, coeff_upper_bound, coeff_lower_bound)

        return final_advs
