import sys
import os

import torch
import torch.nn as nn
import torch.autograd as autograd
import numpy as np
import time
import matplotlib.pyplot as plt

from torch.utils.data import Dataset
from torch.utils.data import DataLoader

from modules.pinn import *
from modules.generate_data import *

class BoundaryDataset(Dataset):
    def __init__(self, x, u, d):
        self.x = []
        self.u = []
        self.d = []

        for a in x:
            self.x.extend(a)
        
        for a in u:
            self.u.extend(a)

        for a in d:
            self.d.extend(a)

    
    def __len__(self):
        return len(self.u)
    
    def __getitem__(self, idx):
        x = torch.FloatTensor(self.x[idx])
        u = torch.FloatTensor(self.u[idx])
        d = self.d[idx]

        return x, u, d

class PDEDataset(Dataset):
    def __init__(self, x, u):
        self.x = x
        self.u = u
    
    def __len__(self):
        return len(self.u)
    
    def __getitem__(self, idx):
        x = torch.FloatTensor(self.x[idx])
        u = torch.FloatTensor(self.u[idx])

        return x, u

def train(model_path, figure_path):
    # Set the starting time
    since = time.time()

    # Set the number of domains
    domain_no = 1

    # Set the global left & right boundary of the calculation domain
    global_lb = -1.0
    global_rb = 1.0

    # Set the size of the overlapping area between domains
    overlap_size = 0.2

    batch_size = 64

    # Initialize combined PINNs
    test = CombinedPINN(domain_no, global_lb, global_rb, overlap_size, figure_path=figure_path)
    sample = {'Model{}'.format(i+1): PINN(i) for i in range(domain_no)}
    test.module_update(sample)
    test.make_domains()
    test.make_boundaries()
    test.make_windows()
    test.plot_domains_and_boundaries()

    # Test windows
    test.plot_windows()

    
    # Prepare to train
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    print("Current device:", device)
    b_size = 100
    f_size = 20000
    epochs = 100000
    # test = nn.DataParallel(test)
    test.to(device)

    # Set boundary conditions
    bcs = []
    bcs.append(BCs(b_size, x=-1.0, u=0.0, deriv=0))
    bcs.append(BCs(b_size, x=1.0, u=0.0, deriv=0))
    bcs.append(BCs(b_size, x=-1.0, u=0.0, deriv=2))
    bcs.append(BCs(b_size, x=1.0, u=0.0, deriv=2))
    bcs.append(BCs(b_size, x=0.5, u=0.0, deriv=0))
    bcs.append(BCs(b_size, x=0.5, u=0.0, deriv=1))
    bcs.append(BCs(b_size, x=-0.5, u=0.0, deriv=0))
    bcs.append(BCs(b_size, x=-0.5, u=0.0, deriv=1))



    optims = []
    schedulers = []

    # models = test._modules['module']._modules
    models = test._modules
    # print(models)

    for key in models.keys():
        model = models[key]
        optim = torch.optim.Adam(model.parameters(), lr=0.001)
        optims.append(optim)
        schedulers.append(torch.optim.lr_scheduler.ReduceLROnPlateau(optim, 'min', patience=100, verbose=True))

    # dms = test.module.domains
    # bds = test.module.boundaries
    dms = test.domains
    bds = test.boundaries

    # Penalty term
    w_b = 100
    w_f = 1

    x_bs = []
    u_bs = []
    x_fs = []
    u_fs = []

    x_derivs = []
    x_derivs_train = [[] for _ in range(domain_no)]

    x_bs_train = [[] for _ in range(domain_no)]
    u_bs_train = [[] for _ in range(domain_no)]
    
    for bc in bcs:
        x_b, u_b = make_training_boundary_data(b_size=bc.size, x=bc.x, u=bc.u)
        x_bs.append(x_b)
        u_bs.append(u_b)
        x_derivs.append(torch.ones(x_b.shape).type(torch.IntTensor) * bc.deriv)


    for i, dm in enumerate(dms):
        lb = dm['lb']
        rb = dm['rb']
        x_f, u_f = make_training_collocation_data(f_size, x_lb=lb, x_rb=rb)
        x_fs.append(x_f)
        u_fs.append(u_f)

        for j, x_b in enumerate(x_bs):
            u_b = u_bs[j]
            x_deriv = x_derivs[j]
            x = x_b[0]
            if lb <= x <= rb:
                x_bs_train[i].append(x_b)
                u_bs_train[i].append(u_b)
                x_derivs_train[i].append(x_deriv)

    loss_save = np.inf
    
    loss_b_plt = [[] for _ in range(domain_no)]
    loss_f_plt = [[] for _ in range(domain_no)]
    loss_plt   = [[] for _ in range(domain_no)]

    x_plt = torch.from_numpy(np.arange((global_rb - global_lb) * 1000) / 1000 + global_lb) 

    for epoch in range(epochs):
        start = time.time()
        for i in range(domain_no):
            start2 = time.time()
            optim = optims[i]
            scheduler = schedulers[i]
            optim.zero_grad()
            loss_b = 0.0
            loss_f = 0.0
            loss_sum = 0.0
            loss_func = nn.MSELoss()
            


            x_bs = x_bs_train[i]
            u_bs = u_bs_train[i]
            x_derivs = x_derivs_train[i]

            boundary_dataset = BoundaryDataset(x_bs, u_bs, x_derivs)
 
            pde_dataset      = PDEDataset(x_bs, u_bs)

            boundary_dataloader = DataLoader(boundary_dataset, batch_size=batch_size, shuffle=True)
            pde_dataloader      = DataLoader(pde_dataset, batch_size=batch_size, shuffle=True)


            # for j, x_b in enumerate(x_bs):
            for j, x_b in enumerate(x_bs):
                u_b = u_bs[j]

                x_b = x_b.cuda()
                u_b = u_b.cuda()
                x_deriv = x_derivs[j]
                loss_b += loss_func(calc_deriv(x_b, test(x_b), x_deriv[0]), u_b) * w_b
            loss_b.backward()
            optim.step()
            # for batch, b_data in enumerate(boundary_dataloader, 1):
            #     # print(batch)
            #     loss_b = 0.0
            #     x_b, u_b, x_deriv = b_data
            #     x_b = x_b.to(device)
            #     u_b = u_b.to(device)
            #     for x, u, d in zip(x_b, u_b, x_deriv):
            #         loss_b += (loss_func(calc_deriv(x, test(x), d), u) * w_b)
            #     loss_b /= batch_size
            #     loss_b.backward(retain_graph=True)
            #     optim.step()

            for batch, f_data in enumerate(pde_dataloader, 1):
                x_f, u_f = f_data
                x_f = x_f.to(device)
                u_f = u_f.to(device)
                loss_f = loss_func(calc_deriv(x_f, test(x_f), 4) - 1, u_f) * w_f
                loss_f.backward()
                optim.step()
                print(batch, x_f.shape)
          
            loss = loss_f.item() + loss_b.item()
            loss_sum += loss

            loss_b_plt[i].append(loss_b.item())
            loss_f_plt[i].append(loss_f.item())
            loss_plt[i].append(loss)
            scheduler.step(loss)
            
            if epoch % 50 == 1:
                draw(domain_no, global_lb, global_rb, overlap_size, model_path, figure_path)
                test.plot_separate_models(x_plt)

            with torch.no_grad():
                test.eval()
            
                print("Epoch: {0} | LOSS: {1:.5f} | LOSS_B: {2:.5f} | LOSS_F: {3:.5f}".format(epoch+1, loss, loss_b.item(), loss_f.item()))

                if epoch % 50 == 1:
                    draw_convergence(epoch + 1, loss_b_plt[i], loss_f_plt[i], loss_plt[i], i, figure_path)

        if loss_sum < loss_save:
            loss_save = loss_sum
            torch.save(test.state_dict(), model_path)
            print(".......model updated (epoch = ", epoch+1, ")")
        
        if loss_sum < 0.0000001:
            break

                
        
        # print("After 1 epoch {:.3f}s".format(time.time() - start))
            
    print("Elapsed Time: {} s".format(time.time() - since))
    print("DONE")
    
def draw_convergence(epoch, loss_b, loss_f, loss, id, figure_path):
    plt.cla()
    x = np.arange(epoch)

    fpath = os.path.join(figure_path, "convergence_model{}.png".format(id))

    plt.plot(x, np.array(loss_b), label='Loss_B')
    plt.plot(x, np.array(loss_f), label='Loss_F')
    plt.plot(x, np.array(loss), label='Loss')
    plt.yscale('log')
    plt.legend()
    plt.savefig(fpath)

def draw(domain_no, lb, rb, overlap_size, model_path, figure_path):
    model = CombinedPINN(domain_no, lb, rb, overlap_size, figure_path)
    sample = {'Model{}'.format(i+1): PINN(i) for i in range(domain_no)}
    model.module_update(sample)
    model.make_domains()
    model.make_boundaries()
    model.cuda()
    print(model_path)
    state_dict = torch.load(model_path)
    state_dict = {m.replace('module.', '') : i for m, i in state_dict.items()}
    model.load_state_dict(state_dict)

    x_test_plt = np.arange(2001)/1000 - 1.0
    x_test = torch.from_numpy(x_test_plt).unsqueeze(0).T.type(torch.FloatTensor).cuda()
    x_test.requires_grad = True
    
    pred = model(x_test).cpu().detach().numpy()

    fpath = os.path.join(figure_path, "model.png")
    plt.cla()
    plt.plot(x_test_plt, pred, 'b', label='CPINN')
    plt.legend()
    plt.savefig(fpath)

    # for i in range(deriv + 1):
        
    #     pred = calc_deriv(x_test, model(x_test), i).cpu().detach().numpy()
    #     plt.cla()
    #     plt.plot(x_test_plt, pred, 'b', label='CPINN')
    #     plt.legend()
    #     plt.savefig('./figures/test_{}.png'.format(i))

def main(model_path, figure_path):
    train(model_path, figure_path)
    # window_test()
    
if __name__ == "__main__":
    since = time.time()
    main(sys.argv[1], sys.argv[2])
    print("Elapsed time: {:.3f} s".format(time.time() - since))
    