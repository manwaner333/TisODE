"""PyTorch Carlini and Wagner L2 attack algorithm.

Based on paper by Carlini & Wagner, https://arxiv.org/abs/1608.04644 and a reference implementation at
https://github.com/tensorflow/cleverhans/blob/master/cleverhans/attacks_tf.py
"""
import os
import sys
import torch
import numpy as np
import torch.nn.functional as F
from numba import jit
from torch import optim
from torch import autograd
from .helpers import *
import scipy.io as scio
import pdb
from .generate_gradient import generate_gradient
from .generate_gradient import cw_grad,spsa_grad
import copy
sys.path.append("../")
# update_pixels = 128
update_pixels = 784
guided = False
GRDSTORE = 0
#@jit(nopython=True)
def coordinate_ADAM(losses, indice, grad, hess, batch_size, mt_arr, vt_arr, real_modifier, up, down, lr, adam_epoch, beta1, beta2, proj):
    
    mt = mt_arr[indice]
    mt = beta1 * mt + (1 - beta1) * grad
    mt_arr[indice] = mt
    vt = vt_arr[indice]
    vt = beta2 * vt + (1 - beta2) * (grad * grad)
    vt_arr[indice] = vt
    epoch = adam_epoch[indice]
    corr = (torch.sqrt(1 - torch.pow(beta2, epoch))) / (1 - torch.pow(beta1, epoch))
    #if self.cuda:
    corr = corr.cuda()
    m = real_modifier.reshape(-1)
    old_val = m[indice]
    old_val -= lr * corr * mt / (torch.sqrt(vt) + 1e-8)
    if proj:
        old_val = torch.max(torch.min(old_val, up[indice]), down[indice])
    m[indice] = old_val
    adam_epoch[indice] = epoch + 1.

class BlackBoxL2:

    def __init__(self, epsilon,learning_rate,targeted = False, search_steps=None, max_steps=None, cuda=True, debug=True,isimgnet = False, isblackbox = False,iscifar = False):
        self.debug = debug
        self.targeted = targeted # false
        self.num_classes = 10 
        self.confidence = 0  # FIXME need to find a good value for this, 0 value used in paper not doing much...
        self.initial_const = 0.5  # bumped up from default of .01 in reference code
        self.binary_search_steps = search_steps or 5
        self.repeat = self.binary_search_steps >= 10
        self.max_steps = max_steps or 1000
        self.abort_early = True
        self.clip_min = -1
        self.clip_max = 1
        self.cuda = cuda
        self.clamp_fn = ''  # set to something else perform a simple clamp instead of tanh
        self.init_rand = False  # an experiment, does a random starting point help?
        self.use_log = True
        self.use_tanh = False
        self.isblackbox = isblackbox
        self.isimgnet = isimgnet
        self.iscifar = iscifar
        self.batch_size = 784
       
        self.use_importance = True
        # self.LEARNING_RATE = 1e-2
        self.epsilon = epsilon
        self.LEARNING_RATE = learning_rate
        self.beta1 = 0
        self.beta2 = 0
        
        self.reset_adam_after_found = False
        self.num_channels = 3
        self.small_x = 32
        self.small_y = 32
        var_size = self.small_x * self.small_y * self.num_channels
        self.use_var_len = var_size
        self.var_list = np.array(range(0, self.use_var_len), dtype = np.int32)
        self.sample_prob = np.ones(var_size, dtype = np.float32) / var_size

        self.mt = torch.zeros(var_size, dtype = torch.float32)
        self.vt = torch.zeros(var_size, dtype = torch.float32)
        self.modifier_up = torch.zeros(var_size, dtype = torch.float32)
        self.modifier_down = torch.zeros(var_size, dtype = torch.float32)
        self.grad = torch.zeros(self.batch_size, dtype = torch.float32)
        self.hess = torch.zeros(self.batch_size, dtype = torch.float32)
        self.adam_epoch = torch.ones(var_size, dtype = torch.float32)

        self.solver_name = 'adam'

        if self.solver_name == 'adam':
            self.solver = coordinate_ADAM
        elif solver != 'fake_zero':
            # print('unknown solver', self.solver_name)
            self.solver = coordinate_ADAM
        # print('Using', self.solver_name, 'solver')

    def _compare(self, output, target):
        if not isinstance(output, (float, int, np.int64)):
            output = np.copy(output)
            if self.targeted:
                # target = (target + 1) % 10
                output[target] -= self.confidence
            else:
                output[target] += self.confidence
            # print(output)
            output = np.argmax(output)
        if self.targeted:
            return output == target
        else:
            return output != target

    def shift_target(self,target):
        label = torch.argmax(target)
        label = (label+1) % 10
        label = label.reshape(1)
        target_onehot = torch.zeros(label.size() + (self.num_classes,))
        if torch.cuda.is_available():
            target_onehot = target_onehot.cuda()
        target_onehot.scatter_(1, label.unsqueeze(1), 1.)
        target_var = autograd.Variable(target_onehot, requires_grad = False) 
        return target_var
 
    def _loss(self, output, target, dist, scale_const):
        # compute the probability of the label class versus the maximum other
        real = (target * output).sum(1)
        other = ((1. - target) * output - target * 10000.).max(1)[0]
        if self.targeted:
            # target_label = self.shift_target(target)
            # other = (target_label * output).sum(1)
  
            if self.use_log:
                loss1 = torch.clamp(torch.log(other + 1e-30) - torch.log(real + 1e-30), min = 0.)
            else:
            # if targeted, optimize for making the other class most likely
                loss1 = torch.clamp(other -real + self.confidence, min = 0.)  # equiv to max(..., 0.)
        else:
            if self.use_log:
                loss1 = torch.clamp(torch.log(real + 1e-30) - torch.log(other + 1e-30), min = 0.)
            else:
            # if non-targeted, optimize for making this class least likely.
                loss1 = torch.clamp(real - other + self.confidence, min=0.)  # equiv to max(..., 0.)
        loss1 = scale_const * loss1
        
        #pdb.set_trace()
        loss2 = dist.squeeze(1)
        #print('loss1 and loss2 is:', loss1, loss2)
        loss = loss1 + loss2
        #pdb.set_trace()
        return loss, loss1, loss2

    def _optimize(self, model, meta_model, step, input_var, modifier_var, target_var, scale_const_var, target, batch_idx, indice, input_orig = None):
        global GRDSTORE

        if self.use_tanh:
            input_adv = (tanh_rescale(modifier_var + input_var, self.clip_min, self.clip_max) +1)/ 2
        else:
            input_adv = modifier_var + input_var
        
        #print('output shape', model(input_adv).shape)
        output = F.softmax(model(input_adv), dim = 1)
        # print('output', output.data[0])
        #print('input min and max', torch.min(input_adv).item(), torch.max(input_adv).item())
        # distance to the original input data
        
        #pdb.set_trace()
        if input_orig is None:
            dist = l2_dist(input_adv, input_var, keepdim = True).squeeze(2).squeeze(2)
        else:
            dist = l2_dist(input_adv, input_orig, keepdim = True).squeeze(2).squeeze(2)

        loss, loss1, loss2 = self._loss(output.data, target_var, dist, scale_const_var)



        indice2 = indice




        input_adv_copy = copy.deepcopy(input_adv.detach())
        


 
    
    
 


        if self.isblackbox:
            meta_output = spsa_grad(model,input_adv,target)
            device = torch.cuda.current_device()
            # meta_output,indice = generate_gradient(device,targeted=False).run(model,input_adv,target)

        else:
            meta_output = cw_grad(model,input_adv,target)
        
        indice = torch.abs(meta_output.data).cpu().numpy().reshape(-1).argsort()
        indice2 = torch.abs(meta_output.data).cpu().numpy().reshape(-1).argsort()
        # indice2 = indice

        grad = meta_output.reshape(-1)[indice2]

        self.solver(loss, indice2, grad, self.hess, self.batch_size, self.mt, self.vt, modifier_var, 
                    self.modifier_up, self.modifier_down, self.LEARNING_RATE, self.adam_epoch, self.beta1, self.beta2, not self.use_tanh)

        loss_np = loss[0].item()
        loss1 = loss1[0].item()
        loss2 = loss2[0].item()
        dist_np = dist[0].data.cpu().numpy()
        output_np = output[0].unsqueeze(0).data.cpu().numpy()
        #pdb.set_trace()
        input_adv_np = input_adv[0].unsqueeze(0).data.permute(0, 2, 3, 1).cpu().numpy()  # back to BHWC for numpy consumption
        #input_adv_np = input_adv.cpu()
        return loss_np, loss1, loss2, dist_np, output_np, input_adv_np, indice

    def run(self, model, meta_model, input, target, batch_idx = 0):
        batch_size, c, h, w = input.size()

        var_size = c * h * w
        # set the lower and upper bounds accordingly
        lower_bound = np.zeros(batch_size)
        scale_const = np.ones(batch_size) * self.initial_const
        upper_bound = np.ones(batch_size) * 1e10

        if not self.use_tanh:
            if self.isimgnet:
                mean = np.array([0.485, 0.456, 0.406])
                std = np.array([0.229, 0.224, 0.225])
                # mean = np.array([0.485, 0.456, 0.406])
                # std = np.array([0.229, 0.224, 0.225])
                mean = torch.tensor(mean).float().cuda()
                std = torch.tensor(std).float().cuda()
                up_bound = torch.ones_like(input)
                down_bound = torch.zeros_like(input)
                for i in range(3):
                    up_bound[:,i,:,:] = (up_bound[:,i,:,:] - mean[i]) / std[i]
                    down_bound[:,i,:,:] = (down_bound[:,i,:,:] - mean[i]) / std[i]


                self.modifier_up = up_bound.reshape(-1) - input.reshape(-1)
                self.modifier_down = down_bound.reshape(-1) - input.reshape(-1)

                epsilon = self.epsilon/0.225
                
                self.modifier_up = torch.min((up_bound.reshape(-1) - input.reshape(-1)), torch.ones_like(input.reshape(-1))*epsilon)
                self.modifier_down = torch.max((down_bound.reshape(-1) - input.reshape(-1)),-1*torch.ones_like(input.reshape(-1))*epsilon)
            elif self.iscifar:
                mean = np.array([0.4914, 0.4822, 0.4465])
                std = np.array([0.2023, 0.1994, 0.2010])
                mean = torch.tensor(mean).float().cuda()
                std = torch.tensor(std).float().cuda()
                up_bound = torch.ones_like(input)
                down_bound = torch.zeros_like(input)
                for i in range(3):
                    up_bound[:,i,:,:] = (up_bound[:,i,:,:] - mean[i]) / std[i]
                    down_bound[:,i,:,:] = (down_bound[:,i,:,:] - mean[i]) / std[i]


                self.modifier_up = up_bound.reshape(-1) - input.reshape(-1)
                self.modifier_down = down_bound.reshape(-1) - input.reshape(-1)

                epsilon = self.epsilon/0.2
                
                self.modifier_up = torch.min((up_bound.reshape(-1) - input.reshape(-1)), torch.ones_like(input.reshape(-1))*epsilon)
                self.modifier_down = torch.max((down_bound.reshape(-1) - input.reshape(-1)),-1*torch.ones_like(input.reshape(-1))*epsilon)
 

            else:
                self.modifier_up = torch.min((1 - input.reshape(-1)),torch.ones_like(input.reshape(-1))*self.epsilon)
                self.modifier_down = torch.max((0 - input.reshape(-1)),-1*torch.ones_like(input.reshape(-1))*self.epsilon)
 

            
        # python/numpy placeholders for the overall best l2, label score, and adversarial image
        o_best_l2 = [1e10] * batch_size
        o_best_score = [-1] * batch_size
        o_best_attack = input.permute(0, 2, 3, 1).cpu().numpy()

    # setup input (image) variable, clamp/scale as necessary
        if self.clamp_fn == 'tanh':
            # convert to tanh-space, input already int -1 to 1 range, does it make sense to do
            # this as per the reference implementation or can we skip the arctanh?
            input_var = autograd.Variable(torch_arctanh(input * 2), requires_grad = False)
            input_orig = tanh_rescale(input_var, self.clip_min, self.clip_max) / 2
        else:
            input_var = autograd.Variable(input, requires_grad=False)
            input_orig = None

        # setup the target variable, we need it to be in one-hot form for the loss function
        target_onehot = torch.zeros(target.size() + (self.num_classes,))
        if self.cuda:
            target_onehot = target_onehot.cuda()
        target_onehot.scatter_(1, target.unsqueeze(1), 1.)
        target_var = autograd.Variable(target_onehot, requires_grad = False)

        # setup the modifier variable, this is the variable we are optimizing over
        modifier = torch.zeros(input_var.size()).float()
        
        self.mt = torch.zeros(var_size, dtype = torch.float32)
        self.vt = torch.zeros(var_size, dtype = torch.float32)
       
        self.adam_epoch = torch.ones(var_size, dtype = torch.float32)
        stage = 0
        eval_costs = 0
        
        if self.init_rand:
            # Experiment with a non-zero starting point...
            modifier = torch.normal(means = modifier, std = 0.001)
        if self.cuda:
            modifier = modifier.cuda()
            
            self.modifier_up = self.modifier_up.cuda()
            self.modifier_down = self.modifier_down.cuda()
            self.mt = self.mt.cuda()
            self.vt = self.vt.cuda()
            self.grad = self.grad.cuda()
            self.hess = self.hess.cuda()

        #modifier_var = autograd.Variable(modifier, requires_grad=True)
        modifier_var = modifier
       
        first_step = 0
        #optimizer = optim.Adam([modifier_var], lr = 0.01, betas = (0.9, 0.999))

        for search_step in range(self.binary_search_steps):
            # print('Batch: {0:>3}, search step: {1}'.format(batch_idx, search_step))
            # if self.debug:
                # print('Const:')
                # for i, x in enumerate(scale_const):
                    # print(i, x)
            best_l2 = [1e10] * batch_size
            best_score = [-1] * batch_size

            # The last iteration (if we run many steps) repeat the search once.
            if self.repeat and search_step == self.binary_search_steps - 1:
                scale_const = upper_bound

            scale_const_tensor = torch.from_numpy(scale_const).float()
            if self.cuda:
                scale_const_tensor = scale_const_tensor.cuda()
            scale_const_var = autograd.Variable(scale_const_tensor, requires_grad = False)

            prev_loss = 1e6
            
            last_loss1 = 1.0
            indice = np.arange(update_pixels)
            #pdb.set_trace()
            for step in range(self.max_steps):
                # perform the attack
                
                loss, loss1, loss2, dist, output, adv_img, indice = self._optimize(
                    model,
                    meta_model,
                    step,
                    input_var,
                    modifier_var,
                    target_var,
                    scale_const_var,
                    target,
                    batch_idx,
                    indice,
                    input_orig)
                #pdb.set_trace()
                if self.solver_name == 'fake_zero':
                    eval_costs += np.prod(modifier.shape)
                else:
                    eval_costs += self.batch_size
                if loss1 == 0.0 and last_loss1 != 0 and stage == 0:
                    if self.reset_adam_after_found:
                        self.mt = torch.zeros(var_size, dtype = torch.float32)
                        self.vt = torch.zeros(var_size, dtype = torch.float32)
                        self.adam_epoch = torch.ones(var_size, dtype = torch.float32)
                    stage = 1
                last_loss1 = loss1

                #print(adv_img.shape, input.shape)
                #print(torch.sum((adv_img-input.cpu())**2)**0.5)
                # if step % 100 == 0 or step == self.max_steps - 1:
                    # print('Step: {0:>4}, loss: {1:6.4f}, loss1: {2:5f}, loss2: {3:5f}, dist: {4:8.5f}, modifier mean: {5:.5e}'.format(
                        # step, loss, loss1, loss2, dist.mean(), modifier_var.data.mean()))

                # if self.abort_early and step % (self.max_steps // 10) == 0:
                if self.abort_early and step % (self.max_steps // 1) == 0:
                    if loss > prev_loss * .9999:
                        break
                    prev_loss = loss

                # update best result found
                for i in range(batch_size):
                    target_label = target[i]
                    output_logits = output[i]
                    output_label = np.argmax(output_logits)
                    di = dist[i]
                    if self.debug:
                        if step % 100 == 0:
                            print('{0:>2} dist: {1:.5f}, output: {2:>3}, {3:5.3}, target {4:>3}'.format(
                                i, di, output_label, output_logits[output_label], target_label))
                    if di < best_l2[i] and self._compare(output_logits, target_label):
                        best_l2[i] = di
                        best_score[i] = output_label
                    if di < o_best_l2[i] and self._compare(output_logits, target_label):
                        # if self.debug:
                            # print('{0:>2} best total, prev dist: {1:.5f}, new dist: {2:.5f}'.format(
                                  # i, o_best_l2[i], di))
                        o_best_l2[i] = di
                        o_best_score[i] = output_label
                        o_best_attack[i] = adv_img[i]
                        first_step = step
                        #modifier_var = torch.zeros(input_var.size()).float().cuda()

                sys.stdout.flush()
                # end inner step loop

            # adjust the constants
            batch_failure = 0
            batch_success = 0
            
            for i in range(batch_size):
                if self._compare(best_score[i], target[i]) and best_score[i] != -1:
                    # successful, do binary search and divide const by two
                    upper_bound[i] = min(upper_bound[i], scale_const[i])
                    if upper_bound[i] < 1e9:
                        scale_const[i] = (lower_bound[i] + upper_bound[i]) / 2
                    if self.debug:
                        print('{0:>2} successful attack, lowering const to {1:.3f}'.format(
                            i, scale_const[i]))
                else:
                    # failure, multiply by 10 if no solution found
                    # or do binary search with the known upper bound
                    lower_bound[i] = max(lower_bound[i], scale_const[i])
                    if upper_bound[i] < 1e9:
                        scale_const[i] = (lower_bound[i] + upper_bound[i]) / 2
                    else:
                        scale_const[i] *= 10
                    if self.debug:
                        print('{0:>2} failed attack, raising const to {1:.3f}'.format(
                            i, scale_const[i]))
                if self._compare(o_best_score[i], target[i]) and o_best_score[i] != -1:
                    batch_success += 1
                else:
                    batch_failure += 1

            # print('Num failures: {0:2d}, num successes: {1:2d}\n'.format(batch_failure, batch_success))
            sys.stdout.flush()
            # end outer search loop

        return o_best_attack, scale_const, first_step
