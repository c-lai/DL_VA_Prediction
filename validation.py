import torch
import time
import sys

import torch
import torch.distributed as dist
import torch.nn.functional as F

from utils import AverageMeter, calculate_accuracy, calculate_accuracy_binary,\
    calculate_precision_and_recall_binary, calculate_auc, \
        get_activation, plot_roc


def val_epoch(epoch,
              data_loader,
              subset_loader,
              model,
              criterion,
              device,
              logger,
              tb_writer=None,
              distributed=False):
    print('validation at epoch {}'.format(epoch))

    model.eval()

    batch_time = AverageMeter()
    data_time = AverageMeter()
    losses = AverageMeter()
    accuracies = AverageMeter()

    end_time = time.time()

    with torch.no_grad():
        outputs_list = []
        probs_list = []
        targets_list = []
        for i, (inputs, targets) in enumerate(data_loader):
            data_time.update(time.time() - end_time)

            targets = targets.to(device, non_blocking=True).view(-1, 1).float()
            targets_list.append(targets)
            outputs = model(inputs)
            outputs_list.append(outputs)
            probs = torch.sigmoid(outputs)
            probs_list.append(probs)
            loss = criterion(outputs, targets)
            acc = calculate_accuracy_binary(probs, targets, balanced=True)

            losses.update(loss.item(), inputs.size(0))
            accuracies.update(acc, inputs.size(0))

            batch_time.update(time.time() - end_time)
            end_time = time.time()

            print('Epoch: [{0}][{1}/{2}]\t'
                  'Time {batch_time.val:.3f} ({batch_time.avg:.3f})\t'
                  'Data {data_time.val:.3f} ({data_time.avg:.3f})\t'
                  'Loss {loss.val:.4f} ({loss.avg:.4f})\t'
                  'Acc {acc.val:.3f} ({acc.avg:.3f})\t'.format(
                        epoch,
                        i + 1,
                        len(data_loader),
                        batch_time=batch_time,
                        data_time=data_time,
                        loss=losses,
                        acc=accuracies))

    outputs_all = torch.cat(outputs_list, dim=0)
    probs_all = torch.cat(probs_list, dim=0)
    targets_all = torch.cat(targets_list, dim=0)
    precision, recall, f1, threshold = calculate_precision_and_recall_binary(probs_all, targets_all)
    auc = calculate_auc(probs_all, targets_all)
    acc = calculate_accuracy_binary(probs_all, targets_all, threshold, balanced=True)
    loss = criterion(outputs_all, targets_all)

    if distributed:
        loss_sum = torch.tensor([losses.sum],
                                dtype=torch.float32,
                                device=device)
        loss_count = torch.tensor([losses.count],
                                  dtype=torch.float32,
                                  device=device)
        acc_sum = torch.tensor([accuracies.sum],
                               dtype=torch.float32,
                               device=device)
        acc_count = torch.tensor([accuracies.count],
                                 dtype=torch.float32,
                                 device=device)

        dist.all_reduce(loss_sum, op=dist.ReduceOp.SUM)
        dist.all_reduce(loss_count, op=dist.ReduceOp.SUM)
        dist.all_reduce(acc_sum, op=dist.ReduceOp.SUM)
        dist.all_reduce(acc_count, op=dist.ReduceOp.SUM)

        losses.avg = loss_sum.item() / loss_count.item()
        accuracies.avg = acc_sum.item() / acc_count.item()

    if logger is not None:
        logger.log({'epoch': epoch, 
                    'loss': loss.item(), 
                    'acc': acc, 
                    'precision': precision,
                    'recall': recall,
                    'f1': f1,
                    'auc': auc,
                    'threshold': threshold})

    if tb_writer is not None:
        tb_writer.add_scalar('val/loss', loss.item(), epoch)
        tb_writer.add_scalar('val/acc', acc, epoch)
        tb_writer.add_scalar('val/precision', precision, epoch)
        tb_writer.add_scalar('val/recall', recall, epoch)
        tb_writer.add_scalar('val/f1', f1, epoch)
        tb_writer.add_scalar('val/auc', auc, epoch)
        tb_writer.add_scalar('val/threshold', threshold, epoch)
        
        if not epoch%10:
            latent_vectors_list = []
            targets_list = []
            for i, (inputs, targets) in enumerate(subset_loader):
                activations = {}
                model.module.fc.register_forward_hook(get_activation(activations, 'fc'))
                outputs = model(inputs)

                latent_vectors_list.append(activations['fc'])
                targets_list.append(targets)
            latent_vectors = torch.cat(latent_vectors_list, dim=0)
            targets_subset = torch.cat(targets_list, dim=0)
            tb_writer.add_embedding(latent_vectors,
                                    metadata=targets_subset,
                                    global_step=epoch,
                                    tag='val/latent space')

    return loss.item()
