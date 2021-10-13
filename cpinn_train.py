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
    


def train(model_path, figure_path):
    # Set the number of domains
    domain_no = 2

    # Set the global left & right boundary of the calculation domain
    global_lb = -1.0
    global_rb = 1.0

    # Batch size
    batch_size = 100

    # Initialize CPINN model
    model = CPINN(domain_no, global_lb, global_rb, figure_path)
    sample = {'Model{}'.format(i+1): PINN(i) for i in range(domain_no)}
    model.module_update(sample)
    model.make_domains()
    model.make_boundaries()
    model.plot_domains()

    # print(model.domains)

    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    print("Current device:", device)

    b_size = 100
    f_size = 10000
    epochs = 10000
    model.to(device)

    bcs = []
    bcs.append(BCs(b_size, x=-1.0, u=0.0, deriv=0))
    bcs.append(BCs(b_size, x=1.0, u=0.0, deriv=0))
    bcs.append(BCs(b_size, x=-1.0, u=0.0, deriv=2))
    bcs.append(BCs(b_size, x=1.0, u=0.0, deriv=2))
    bcs.append(BCs(b_size, x=0.0, u=0.0, deriv=0))
    bcs.append(BCs(b_size, x=0.0, u=0.0, deriv=1))

    optims = []
    schedulers = []

    models = model._modules

    for key in models.keys():
        sub_model = models[key]
        optim = torch.optim.Adam(sub_model.parameters(), lr=0.001)
        optims.append(optim)
        schedulers.append(torch.optim.lr_scheduler.ReduceLROnPlateau(optim, 'min', patience=100, verbose=True))

    dms = model.domains
    
    w_b = 100
    w_f = 1
    w_i = 100

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
    loss_i_plt = [[] for _ in range(domain_no)]
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
            loss_i = 0.0
            loss_sum = 0.0
            loss_func = nn.MSELoss()
            


            x_bs = x_bs_train[i]
            u_bs = u_bs_train[i]
            x_derivs = x_derivs_train[i]

            # print(x_bs)

            boundary_dataset = BoundaryDataset(x_bs, u_bs, x_derivs)
 
            pde_dataset      = PDEDataset(x_fs, u_fs)
            # pde_dataset      = PDEDataset(x_bs, u_bs)
            # print(len(pde_dataset))

            boundary_dataloader = DataLoader(boundary_dataset, batch_size=batch_size, shuffle=True)
            pde_dataloader      = DataLoader(pde_dataset, batch_size=batch_size, shuffle=False)


            for batch, f_data in enumerate(pde_dataloader, 1):
                loss_b = 0.0
                loss_f = 0.0
                for j, x_b in enumerate(x_bs):
                    u_b = u_bs[j]

                    x_b = x_b.cuda()
                    u_b = u_b.cuda()
                    x_deriv = x_derivs[j]
                    loss_b += loss_func(calc_deriv(x_b, model(x_b), x_deriv[0]), u_b) * w_b

                x_f, u_f = f_data
                x_f = x_f.to(device)
                u_f = u_f.to(device)
                loss_f = loss_func(calc_deriv(x_f, model(x_f), 4) - 1, u_f) * w_f

                loss_i = model.get_boundary_error() * w_i

                loss = loss_b + loss_f + loss_i
                loss.backward()
                optim.step()
                # print(batch, x_f.shape)
          
            loss = loss_f.item() + loss_b.item()
            loss_sum += loss

            loss_b_plt[i].append(loss_b.item())
            loss_f_plt[i].append(loss_f.item())
            loss_i_plt[i].append(loss_i.item())
            loss_plt[i].append(loss)
            scheduler.step(loss)
            
            if epoch % 50 == 1:
                model.plot_model(x_plt)
                model.plot_separate_models(x_plt)

            with torch.no_grad():
                model.eval()
            
                print("Epoch: {0} | LOSS: {1:.5f} | LOSS_B: {2:.5f} | LOSS_F: {3:.5f} | LOSS_I: {4:.5f}".format(epoch+1, loss, loss_b.item(), loss_f.item(), loss_i.item()))

                if epoch % 50 == 1:
                    draw_convergence_cpinn(epoch + 1, loss_b_plt[i], loss_f_plt[i], loss_i_plt[i], loss_plt[i], i, figure_path)

        if loss_sum < loss_save:
            loss_save = loss_sum
            torch.save(model.state_dict(), model_path)
            print(".......model updated (epoch = ", epoch+1, ")")
        
        # if loss_sum < 0.0000001:
        #     break

                
        
        # print("After 1 epoch {:.3f}s".format(time.time() - start))
            
    print("DONE")

    

def main(model_path, figure_path):
    since = time.time()
    train(model_path, figure_path)
    print("Elapsed time: {:.3f} s".format(time.time() - since))

if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])