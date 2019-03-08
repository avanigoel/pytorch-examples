"""
RankNet:
From RankNet to LambdaRank to LambdaMART: An Overview
https://www.microsoft.com/en-us/research/wp-content/uploads/2016/02/MSR-TR-2010-82.pdf

Pairwise RankNet:
During training, the NN takes in a pair of positive example and negative
example, the RankNet compute the positive example's score, and negative example
score, the difference is sent to a sigmoid function.
The loss function can use cross entropy loss.
"""

import os

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F

from load_mslr import get_time
from utils import (
    eval_cross_entropy_loss,
    eval_ndcg_at_k,
    get_device,
    get_ckptdir,
    init_weights,
    load_train_vali_data,
    get_args_parser,
    save_to_ckpt,
)


class RankNet(nn.Module):
    def __init__(self, net_structures, double_precision=False):
        """
        :param net_structures: list of int for RankNet FC width
        """
        super(RankNet, self).__init__()
        self.fc_layers = len(net_structures)
        for i in range(len(net_structures) - 1):
            layer = nn.Linear(net_structures[i], net_structures[i+1])
            if double_precision:
                layer = layer.double()
            setattr(self, 'fc' + str(i + 1), layer)

        last_layer = nn.Linear(net_structures[-1], 1)
        if double_precision:
            last_layer = last_layer.double()
        setattr(self, 'fc' + str(len(net_structures)), last_layer)
        self.activation = nn.ReLU6()

    def forward(self, input1):
        for i in range(1, self.fc_layers):
            fc = getattr(self, 'fc' + str(i))
            input1 = F.relu(fc(input1))

        fc = getattr(self, 'fc' + str(self.fc_layers))
        return self.activation(fc(input1))

    def dump_param(self):
        for i in range(1, self.fc_layers + 1):
            print("fc{} layers".format(i))
            fc = getattr(self, 'fc' + str(i))

            with torch.no_grad():
                weight_norm, weight_grad_norm = torch.norm(fc.weight).item(), torch.norm(fc.weight.grad).item()
                bias_norm, bias_grad_norm = torch.norm(fc.bias).item(), torch.norm(fc.bias.grad).item()
            try:
                weight_ratio = weight_grad_norm / weight_norm if weight_norm else float('inf') if weight_grad_norm else 0.0
                bias_ratio = bias_grad_norm / bias_norm if bias_norm else float('inf') if bias_grad_norm else 0.0
            except Exception:
                import ipdb; ipdb.set_trace()

            print(
                '\tweight norm {:.4e}'.format(weight_norm), ', grad norm {:.4e}'.format(weight_grad_norm),
                ', ratio {:.4e}'.format(weight_ratio),
                # 'weight type {}, weight grad type {}'.format(fc.weight.type(), fc.weight.grad.type())
            )
            print(
                '\tbias norm {:.4e}'.format(bias_norm), ', grad norm {:.4e}'.format(bias_grad_norm),
                ', ratio {:.4e}'.format(bias_ratio),
                # 'bias type {}, bias grad type {}'.format(fc.bias.type(), fc.bias.grad.type())
            )


class RankNetPairs(RankNet):
    def __init__(self, net_structures, double_precision=False):
        super(RankNetPairs, self).__init__(net_structures, double_precision)

    def forward(self, input1, input2):
        # from 1 to N - 1 layer, use ReLU as activation function
        for i in range(1, self.fc_layers):
            fc = getattr(self, 'fc' + str(i))
            input1 = F.relu(fc(input1))
            input2 = F.relu(fc(input2))

        # last layer use ReLU6 Activation Function
        fc = getattr(self, 'fc' + str(self.fc_layers))
        input1 = self.activation(fc(input1))
        input2 = self.activation(fc(input2))

        # normalize input1 - input2 as a probability that doc1 should rank higher than doc2
        return torch.sigmoid(input1 - input2)


# define training algo:
SUM_SESSION = "sum_session"
ACC_GRADIENT = "accelerate_grad"
BASELINE = "baseline"


#############################
# Train RankNet with Different Algorithms
#############################
def train_rank_net(
    start_epoch=0, additional_epoch=100, lr=0.0001, optim="adam",
    train_algo=SUM_SESSION,
    double_precision=False, standardize=False,
    small_dataset=False, debug=False
):
    """

    :param start_epoch: int
    :param additional_epoch: int
    :param lr: float
    :param optim: str
    :param train_algo: str
    :param double_precision: boolean
    :param standardize: boolean
    :param small_dataset: boolean
    :param debug: boolean
    :return:
    """
    print("start_epoch:{}, additional_epoch:{}, lr:{}".format(start_epoch, additional_epoch, lr))
    precision = torch.float64 if double_precision else torch.float32

    # get training and validation data:
    data_fold = 'Fold1'
    train_loader, df_train, valid_loader, df_valid = load_train_vali_data(data_fold, small_dataset)
    if standardize:
        df_train, scaler = train_loader.train_scaler_and_transform()
        df_valid = valid_loader.apply_scaler(scaler)

    net, net_inference, ckptfile = get_train_inference_net(
        train_algo, train_loader.num_features, start_epoch, double_precision
    )
    device = get_device()
    net.to(device)
    net_inference.to(device)

    # initialize to make training faster
    net.apply(init_weights)

    if optim == "adam":
        optimizer = torch.optim.Adam(net.parameters(), lr=lr)
    elif optim == "sgd":
        optimizer = torch.optim.SGD(net.parameters(), lr=lr, momentum=0.9)
    else:
        raise ValueError("Optimization method {} not implemented".format(optim))
    print(optimizer)

    scheduler = torch.optim.lr_scheduler.StepLR(optimizer, step_size=10, gamma=0.75)

    if train_algo == BASELINE:
        loss_func = torch.nn.BCELoss()
        loss_func.to(device)

    losses = []

    for i in range(start_epoch, start_epoch + additional_epoch):

        scheduler.step()
        net.zero_grad()
        net.train()

        if train_algo == BASELINE:
            epoch_loss = baseline_pairwise_training_loop(
                i, net, loss_func, optimizer,
                train_loader,
                precision=precision, device=device, debug=debug
            )
        elif train_algo == SUM_SESSION:
            epoch_loss = factorized_training_loop(
                i, net, None, optimizer,
                train_loader,
                precision=precision, device=device, debug=debug
            )

        losses.append(epoch_loss)
        print('=' * 20 + '\n', get_time(), 'Epoch{}, loss : {}'.format(i, losses[-1]), '\n' + '=' * 20)

        # save to checkpoint every 5 step, and run eval
        if i % 5 == 0 and i != start_epoch:
            save_to_ckpt(ckptfile, i, net, optimizer, scheduler)
            net_inference.load_state_dict(net.state_dict())
            eval_model(net_inference, device, df_valid, valid_loader)

    # save the last ckpt
    save_to_ckpt(ckptfile, start_epoch + additional_epoch, net, optimizer, scheduler)

    # save the final model
    torch.save(net.state_dict(), ckptfile)


def get_train_inference_net(train_algo, num_features, start_epoch, double_precision):
    ranknet_structure = [num_features, 64, 16]

    if train_algo == BASELINE:
        net = RankNetPairs(ranknet_structure, double_precision)
        net_inference = RankNet(ranknet_structure)  # inference always use single precision
        ckptfile = get_ckptdir('ranknet', ranknet_structure)

    elif train_algo in [SUM_SESSION, ACC_GRADIENT]:
        net = RankNet(ranknet_structure, double_precision)
        net_inference = net
        ckptfile = get_ckptdir('ranknet-factorize', ranknet_structure)

    else:
        raise ValueError("train algo {} not implemented".format(train_algo))

    if start_epoch != 0:
        load_from_ckpt(ckptfile, start_epoch, net)

    return net, net_inference, ckptfile


def baseline_pairwise_training_loop(
    epoch, net, loss_func, optimizer,
    train_loader, batch_size=100000,
    precision=torch.float32, device="cpu",
    debug=False
):
    minibatch_loss = []
    minibatch = 0
    count = 0

    for x_i, y_i, x_j, y_j in train_loader.generate_query_pair_batch(batch_size):
        if x_i is None or x_i.shape[0] == 0:
            continue
        x_i, x_j = torch.tensor(x_i, dtype=precision, device=device), torch.tensor(x_j, dtype=precision, device=device)
        # binary label
        y = torch.tensor((y_i > y_j).astype(np.float32), dtype=precision, device=device)

        net.zero_grad()

        y_pred = net(x_i, x_j)
        loss = loss_func(y_pred, y)

        loss.backward()
        count += 1
        if count % 25 == 0 and debug:
            net.dump_param()
        optimizer.step()

        minibatch_loss.append(loss.item())

        minibatch += 1
        if minibatch % 100 == 0:
            print(get_time(), 'Epoch {}, Minibatch: {}, loss : {}'.format(epoch, minibatch, loss.item()))

    return np.mean(minibatch_loss)


def factorized_training_loop(
    epoch, net, loss_func, optimizer,
    train_loader, batch_size=100, sigma=1.0,
    training_algo=SUM_SESSION,
    precision=torch.float32, device="cpu",
    debug=False
):
    minibatch_loss = []
    count, loss, pairs = 0, 0, 0
    grad_batch, y_pred_batch = [], []
    for X, Y in train_loader.generate_batch_per_query():
        if X is None or X.shape[0] == 0:
            continue
        Y_tensor = torch.tensor(Y, dtype=precision, device=device).view(-1, 1)
        rel_diff = Y_tensor - Y_tensor.t()
        pos_pairs = (rel_diff > 0).type(precision)
        neg_pairs = (rel_diff < 0).type(precision)

        num_pairs = torch.sum(pos_pairs, (0, 1)) + torch.sum(neg_pairs, (0, 1))
        # skip negative sessions, no relevant info:
        if num_pairs == 0:
            continue

        X_tensor = torch.tensor(X, dtype=precision, device=device)
        y_pred = net(X_tensor)

        if training_algo == SUM_SESSION:
            C_pos = torch.log(1 + torch.exp(-sigma * (y_pred - y_pred.t())))
            C_neg = torch.log(1 + torch.exp(sigma * (y_pred - y_pred.t())))

            C = pos_pairs * C_pos + neg_pairs * C_neg
            loss += torch.sum(C, (0, 1))
        elif training_algo == ACC_GRADIENT:
            y_pred_batch.append(y_pred)
            with torch.no_grad():
                l = - (pos_pairs - neg_pairs) / (1 + torch.exp(y_pred - y_pred.t()))
                loss += torch.sum(
                    torch.log(1 + torch.exp(-sigma * (y_pred - y_pred.t()))) * pos_pairs +
                    torch.log(1 + torch.exp(sigma * (y_pred - y_pred.t()))) * neg_pairs,
                    (0, 1)
                )
                back = torch.sum(l / num_pairs, dim=1, keepdim=True)

                if torch.sum(back, dim=(0, 1)) == float('inf') or back.shape != y_pred.shape:
                    import ipdb; ipdb.set_trace()
                grad_batch.append(back)
        else:
            raise ValueError("training algo {} not implemented".format(training_algo))

        pairs += num_pairs
        count += 1

        if count % batch_size == 0:
            loss /= pairs
            minibatch_loss.append(loss.item())
            if training_algo == SUM_SESSION:
                print("Epoch {}, number of pairs {}, loss {}".format(epoch, pairs, loss.item()))
                loss.backward()
            elif training_algo == ACC_GRADIENT:
                for grad, y_pred in zip(grad_batch, y_pred_batch):
                    y_pred.backward(grad / pairs)

            if count % (4 * batch_size) and debug:
                net.dump_param()

            optimizer.step()
            net.zero_grad()
            loss, pairs = 0, 0  # loss used for sum_session
            grad_batch, y_pred_batch = [], []  # grad_batch, y_pred_batch used for gradient_acc

    if pairs:
        print('+' * 10, "End of batch, remaining pairs {}".format(pairs.item()))
        loss /= pairs
        minibatch_loss.append(loss.item())
        if training_algo == SUM_SESSION:
            loss.backward()
        else:
            for grad, y_pred in zip(grad_batch, y_pred_batch):
                y_pred.backward(grad / pairs)

        if debug:
            net.dump_param()
        optimizer.step()

        return np.mean(minibatch_loss)


def eval_model(inference_model, device, df_valid, valid_loader):
    """
    :param model: torch.nn.Module
    :param inference_model: torch.nn.Module
    :param loss_func: loss function
    :param device: str, cpu or cuda:id
    :param df_valid: pandas.DataFrame with validation data
    :param valid_loader:
    :return:
    """
    inference_model.eval()  # Set model to evaluate mode
    batch_size = 1000000

    with torch.no_grad():
        eval_cross_entropy_loss(inference_model, device, valid_loader)
        eval_ndcg_at_k(inference_model, device, df_valid, valid_loader, batch_size, [10, 30])


def load_from_ckpt(ckpt_file, epoch, model):
    ckpt_file = ckpt_file + '_{}'.format(epoch)
    if os.path.isfile(ckpt_file):
        print(get_time(), 'load from ckpt {}'.format(ckpt_file))
        ckpt_state_dict = torch.load(ckpt_file)
        model.load_state_dict(ckpt_state_dict['model_state_dict'])
        print(get_time(), 'finish load from ckpt {}'.format(ckpt_file))
    else:
        print('ckpt file does not exist {}'.format(ckpt_file))


if __name__ == "__main__":
    parser = get_args_parser()
    # add additional args for RankNet
    parser.add_argument(
        "--train_algo", dest="train_algo", default=SUM_SESSION,
        choices=[SUM_SESSION, ACC_GRADIENT, BASELINE],
        help=(
            "{}: Loss func sum on the session level,".format(SUM_SESSION) +
            "{}: compute gradient on session level, ".format(ACC_GRADIENT) +
            "{}: Loss func some on pairs".format(BASELINE)
        )
    )
    args = parser.parse_args()
    train_rank_net(
        args.start_epoch, args.additional_epoch, args.lr, args.optim,
        args.train_algo,
        args.double_precision, args.standardize,
        args.small_dataset, args.debug,
    )
