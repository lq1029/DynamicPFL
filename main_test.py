import os
import torch
import torch.nn as nn
import torch.optim as optim
from torchvision import datasets, transforms
from torch.utils.data import DataLoader, Subset
# from opacus import PrivacyEngine
from options import parse_args
from data import *
from net import *
from tqdm import tqdm
from utils import compute_noise_multiplier, compute_fisher_diag
from tqdm.auto import trange
import copy
import sys
import random
# from torch.utils.tensorboard import SummaryWriter

args = parse_args()
os.environ["CUDA_VISIBLE_DEVICES"] = str(args.device)
num_clients = args.num_clients
local_epoch = args.local_epoch
global_epoch = args.global_epoch
batch_size = args.batch_size
target_epsilon = args.target_epsilon
target_delta = args.target_delta
clipping_bound = args.clipping_bound
dataset = args.dataset
user_sample_rate = args.user_sample_rate
# device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
device = torch.device("cuda" if torch.cuda.is_available() else "mps")
if args.store:
    saved_stdout = sys.stdout
    file = open(
        f'./txt/{args.dirStr}/'
        f'dataset {dataset} '
        f'--num_clients {num_clients} '
        f'--user_sample_rate {args.user_sample_rate} '
        f'--local_epoch {local_epoch} '
        f'--global_epoch {global_epoch} '
        f'--batch_size {batch_size} '
        f'--target_epsilon {target_epsilon} '
        f'--target_delta {target_delta} '
        f'--clipping_bound {clipping_bound} '
        f'--fisher_threshold {args.fisher_threshold} '
        f'--lambda_1 {args.lambda_1} '
        f'--lambda_2 {args.lambda_2} '
        f'--lr {args.lr} '
        f'--alpha {args.dir_alpha}'
        f'.txt'
        , 'a'
    )
    sys.stdout = file

# writer = SummaryWriter(log_dir='logs')


def local_update(model, dataloader):
    model.train()
    model = model.to(device)
    optimizer = optim.Adam(params=model.parameters(), lr=args.lr)
    loss_fn = nn.CrossEntropyLoss()
    total_loss = 0
    for _ in range(local_epoch):
        for data, labels in dataloader:
            data, labels = data.to(device), labels.to(device)
            optimizer.zero_grad()
            outputs = model(data)
            loss = loss_fn(outputs, labels)
            total_loss += loss
            loss.backward()
            optimizer.step()
    # model = model.to('cpu')
    # model = model.to(device)
    avg_loss = total_loss.item() / len(dataloader)
    # return model
    return avg_loss


def test(client_model, client_testloader):
    client_model.eval()
    client_model = client_model.to(device)

    loss_fn = nn.CrossEntropyLoss()
    total_loss = 0

    num_data = 0

    correct = 0
    with torch.no_grad():
        for data, labels in client_testloader:
            data, labels = data.to(device), labels.to(device)
            outputs = client_model(data)
            _, predicted = torch.max(outputs, 1)
            correct += (predicted == labels).sum().item()
            num_data += labels.size(0)

            loss = loss_fn(outputs, labels)
            total_loss += loss

    avg_loss = total_loss / len(client_testloader.dataset)
    accuracy = 100.0 * correct / num_data

    # client_model = client_model.to('cpu')

    return accuracy


def calculate_percentage_of_ones(params_list):
    total_ones = sum(torch.sum(param == 1).item() for param in params_list)
    total_elements = sum(param.numel() for param in params_list)
    percentage = total_ones / total_elements
    return percentage


def main():
    mean_acc_s = []
    mean_loss_s = []
    acc_matrix = []
    if dataset == 'MNIST':
        train_dataset, test_dataset = get_mnist_datasets()
        clients_train_set = get_clients_datasets(train_dataset, num_clients)
        client_data_sizes = [len(client_dataset) for client_dataset in clients_train_set]
        clients_train_loaders = [DataLoader(client_dataset, batch_size=batch_size) for client_dataset in
                                 clients_train_set]
        clients_test_loaders = [DataLoader(test_dataset) for i in range(num_clients)]
        clients_models = [mnistNet() for _ in range(num_clients)]
        global_model = mnistNet()
    elif dataset == 'CIFAR10':
        clients_train_loaders, clients_test_loaders, client_data_sizes = get_CIFAR10(args.dir_alpha, num_clients)
        clients_models = [cifar10Net() for _ in range(num_clients)]
        global_model = cifar10Net().to(device)
    elif dataset == 'FEMNIST':
        # clients_train_loaders, clients_test_loaders, client_data_sizes = get_FEMNIST(num_clients)
        clients_train_loaders, clients_test_loaders, client_data_sizes = get_EMNIST(num_clients)
        clients_models = [femnistNet() for _ in range(num_clients)]
        global_model = femnistNet().to(device)
    elif dataset == 'SVHN':
        clients_train_loaders, clients_test_loaders, client_data_sizes = get_SVHN(args.dir_alpha, num_clients)
        clients_models = [SVHNNet() for _ in range(num_clients)]
        global_model = SVHNNet().to(device)
    else:
        print('undifined dataset')
        assert 1 == 0
    for client_model in clients_models:
        client_model.load_state_dict(global_model.state_dict())
    noise_multiplier = compute_noise_multiplier(target_epsilon, target_delta, global_epoch, local_epoch, batch_size,
                                                client_data_sizes)
    print(noise_multiplier)
    if args.no_noise:
        noise_multiplier = 0

    for epoch in trange(global_epoch):
        sampled_client_indices = random.sample(range(num_clients), max(1, int(user_sample_rate * num_clients)))
        sampled_clients_models = [clients_models[i] for i in sampled_client_indices]
        sampled_clients_train_loaders = [clients_train_loaders[i] for i in sampled_client_indices]
        sampled_clients_test_loaders = [clients_test_loaders[i] for i in sampled_client_indices]
        clients_model_updates = []
        clients_accuracies = []
        clients_true_params = []
        clients_false_params = []
        loss = []

        # local training
        for idx, (client_model, client_trainloader, client_testloader) in enumerate(
                zip(sampled_clients_models, sampled_clients_train_loaders, sampled_clients_test_loaders)):
            if not args.store:
                tqdm.write(f'client:{idx + 1}/{args.num_clients}')
            avg_loss = local_update(client_model, client_trainloader)
            loss.append(avg_loss)

            # client_model经过local_update内部可设置GPU/CPU;global_model也需要指定GPU/CPU
            client_update = [param.data - global_weight for param, global_weight in
                             zip(client_model.parameters(), global_model.parameters())]
            clients_model_updates.append(client_update)
            accuracy = test(client_model, client_testloader)
            clients_accuracies.append(accuracy)

            # 计算、保存每一个client端的fisher矩阵，以供噪声添加时候，依据fisher数值来选择添加
            # mps：client_model;
            # CUDA：client_model.to(device)
            fisher_diag = compute_fisher_diag(client_model, client_trainloader)
            fisher_diag_threshold = args.fisher_threshold
            true_params = []
            false_params = []
            for param, fisher_value in zip(client_model.parameters(), fisher_diag):
                # true_param = (fisher_value >= fisher_diag_threshold).int()
                # false_param = (fisher_value < fisher_diag_threshold).int()
                # true_param = torch.ones_like(param) * (fisher_value >= fisher_diag_threshold).clone().detach()
                false_param = torch.ones_like(param) * (fisher_value <= fisher_diag_threshold).clone().detach()

                # true_params.append(true_param)
                false_params.append(false_param)
            clients_true_params.append(true_params)
            clients_false_params.append(false_params)

        # 计算所有客户端中1的数目占比，即<阈值fisher_threshold的占比
        clients_percentage = [calculate_percentage_of_ones(client_params) for client_params in clients_false_params]

        # 打印每个客户端的占比
        for idx, percentage in enumerate(clients_percentage):
            print(f"Client {idx + 1} percentage of ones: {percentage * 100:.2f}%")

        # 打印平均占比
        print(sum(clients_percentage) / len(clients_percentage))

        print(clients_accuracies)
        mean_acc_s.append(sum(clients_accuracies) / len(clients_accuracies))
        mean_loss_s.append(sum(loss) / len(loss))

        # writer.add_scalar('Train/loss', sum(loss) / len(loss), epoch)
        # writer.add_scalar('train/acc', sum(clients_accuracies) / len(clients_accuracies), epoch)

        print(f"mean_acc: {mean_acc_s}, mean_loss: {mean_loss_s}")
        acc_matrix.append(clients_accuracies)
        sampled_client_data_sizes = [client_data_sizes[i] for i in sampled_client_indices]
        sampled_client_weights = [
            sampled_client_data_size / sum(sampled_client_data_sizes)
            for sampled_client_data_size in sampled_client_data_sizes
        ]
        clipped_updates = []
        noisy_updates = []

        # clipped & noise add
        '''
        for idx, client_update in enumerate(clients_model_updates):
            if not args.no_clip:
                norm = torch.sqrt(sum([torch.sum(param ** 2) for param in client_update]))
                clip_rate = max(1, (norm / clipping_bound))
                clipped_update = [(param / clip_rate) for param in client_update]
            else:
                clipped_update = client_update
            clipped_updates.append(clipped_update)

        for idx, (clipped_update, false_params) in enumerate(zip(clipped_updates, clients_false_params)):
            noise_stddev = torch.sqrt(torch.tensor((clipping_bound ** 2) * (noise_multiplier ** 2) / num_clients))
            noise = [param * noise_stddev for param in false_params]
            print(f"clipped_update: {clipped_update[0].shape}, noise: {noise[0].shape}")
            noisy_update = [clipped_param + noise_param for clipped_param, noise_param in
                            zip(clipped_update, noise)]
            noisy_updates.append(noisy_update)
        '''

        # '''
        for idx, (client_update, false_params) in enumerate(zip(clients_model_updates, clients_false_params)):
            if not args.no_clip:
                norm = torch.sqrt(sum([torch.sum(param ** 2) for param in client_update]))
                clip_rate = max(1, (norm / clipping_bound))
                clipped_update = [(param / clip_rate) for param in client_update]
            else:
                clipped_update = client_update
            clipped_updates.append(clipped_update)

            noise_stddev = torch.sqrt(torch.tensor((clipping_bound ** 2) * (noise_multiplier ** 2) / num_clients))

            # todo 在差分噪声的基础上 * 过滤矩阵，而非使用过滤矩阵 * 噪声标准差来生成噪声; 正确做法是:
            noise = [torch.randn_like(param) * noise_stddev * param for param in false_params]
            # noise = [param * noise_stddev for param in false_params]

            # print(f"Shapes - clipped_update: {clipped_update[0].shape},noise_param: {noise[0].shape}")

            noisy_update = [clipped_param + noise_param for clipped_param, noise_param in zip(clipped_update, noise)]
            noisy_updates.append(noisy_update)
        # '''

        aggregated_update = [
            torch.sum(
                torch.stack(
                    [
                        noisy_update[param_index] * sampled_client_weights[idx]
                        for idx, noisy_update in enumerate(noisy_updates)
                    ]
                ),
                dim=0,
            )
            for param_index in range(len(noisy_updates[0]))
        ]
        with torch.no_grad():
            for global_param, update in zip(global_model.parameters(), aggregated_update):
                global_param.add_(update)
        for client_model in clients_models:
            client_model.load_state_dict(global_model.state_dict())

    char_set = '1234567890abcdefghijklmnopqrstuvwxyz'
    ID = ''
    for ch in random.sample(char_set, 5):
        ID = f'{ID}{ch}'

    # todo loss曲线 & save file

    print(
        f'===============================================================\n'
        f'task_ID : '
        f'{ID}\n'
        f'main_test\n'
        f'noise_multiplier : {noise_multiplier}\n'
        f'mean accuracy : \n'
        f'{mean_acc_s}\n'
        f'mean loss : \n'
        f'{mean_loss_s}\n'
        f'acc matrix : \n'
        f'{torch.tensor(acc_matrix)}\n'
        f'===============================================================\n'
    )

    directory_path = './logs/{}_eps_{}_txt'.format(dataset, target_epsilon)
    os.makedirs(directory_path, exist_ok=True)
    with open(directory_path + '/dynamicDP.txt', 'a') as temp_file:
        temp_file.write(f"mean_acc{mean_acc_s}\n"
                        f"mean_loss{mean_loss_s}")


if __name__ == '__main__':
    main()
