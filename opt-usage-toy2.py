# -*- coding: utf-8 -*-
"""
Created on Wed Oct  6 18:50:32 2021

@author: Rounak Meyur and Swapna Thorve
"""

import sys,os
import numpy as np
import networkx as nx
import pandas as pd
from math import ceil
from shapely.geometry import LineString

workpath = os.getcwd()
rootpath = os.path.dirname(workpath)
libpath = workpath + "/libs/"
inppath = workpath + "/input/"
figpath = workpath + "/figs/"
outpath = workpath + "/out/"
distpath = rootpath + "/synthetic-distribution/primnet/out/osm-primnet/"
grbpath = workpath + "/gurobi/"
homepath = inppath + "VA121_20140723.csv"


sys.path.append(libpath)
from pyLoadlib import Load
print("Imported modules")



#%% Home definition

def get_home_data(home_filename):
    # Extract residence device data
    df_homes = pd.read_csv(home_filename)
    home_rawdata = df_homes.set_index('hid').T.to_dict()
    home_data = {h: {"PV":{},"ESS":{},"SL":{},"TCL":{},"FIXED":{},"ORG":{}} \
                 for h in home_rawdata}
    
    for i,h in enumerate(home_rawdata):
        # Schedulable loads
        if home_rawdata[h]["hasCw"]==1:
            time = home_rawdata[h]["minutesCd"] + home_rawdata[h]["minutesCw"]
            rating = ((home_rawdata[h]["wattageCd"]*home_rawdata[h]["minutesCd"])\
                      + (home_rawdata[h]["wattageCw"]*home_rawdata[h]["minutesCw"])) / time
            home_data[h]["SL"]["laundry"] = {"rating":rating/1000.0,
                                             "time":ceil(time/60.0)}
        if home_rawdata[h]["hasDw"]==1:
            time = home_rawdata[h]["minutesDw"]
            rating = home_rawdata[h]["wattageDw"]
            home_data[h]["SL"]["dwasher"] = {"rating":rating/1000.0,
                                                "time":ceil(time/60.0)}
        # if home_rawdata[h]["hasCw"]==1:
        #     home_data[h]["SL"]["laundry"] = {"rating":1.5,"time":2}
        # if home_rawdata[h]["hasDw"]==1:
        #     home_data[h]["SL"]["dwasher"] = {"rating":0.9,"time":2}
        
        # Fixed loads
        home_data[h]["FIXED"]["base"] = [home_rawdata[h]["base_load_"+str(i+1)] \
                                         for i in range(24)]
        home_data[h]["FIXED"]["hvac"] = [home_rawdata[h]["hvac_kwh_"+str(i+1)] \
                                         for i in range(24)]
        home_data[h]["FIXED"]["hoth2o"] = [home_rawdata[h]["hoth2o_kwh_"+str(i+1)] \
                                         for i in range(24)]
        
        # Original Schedulable load profile
        home_data[h]["ORG"]["dwasher"] = [home_rawdata[h]["dwasher_kwh_"+str(i+1)]\
                                          for i in range(24)]
        home_data[h]["ORG"]["laundry"] = [home_rawdata[h]["laundry_kwh_"+str(i+1)]\
                                          for i in range(24)]
        home_data[h]["ORG"]["total"] = [home_data[h]["ORG"]["dwasher"][t] \
            + home_data[h]["ORG"]["laundry"][t] + home_data[h]["FIXED"]["base"][t] \
                + home_data[h]["FIXED"]["hvac"][t] + home_data[h]["FIXED"]["hoth2o"][t] \
                    for t in range(24)]
    return home_data

all_homes = get_home_data(homepath)

# Store the home data for the following houses with IDs from the toy network
homeid = [511210207001189,51121020900342,511210203001855,511210207001113,
          51121020900346,51121021300494]



#%% Optimize power usage schedule
def compute_Rmat(graph):
    A = nx.incidence_matrix(graph,nodelist=list(graph.nodes()),
                            edgelist=list(graph.edges()),oriented=True).toarray()
    node_ind = [i for i,node in enumerate(graph.nodes()) \
                if graph.nodes[node]['label'] != 'S']
    
    # Resistance data
    F = np.linalg.inv(A[node_ind,:].T)
    D = np.diag([graph.edges[e]['r'] for e in graph.edges])
    return F@D@(F.T)

def compute_power_schedule(net,homes,cost,feed):
    homelist = [n for n in net if net.nodes[n]['label'] == 'H']
    incentive = nx.get_node_attributes(net,"alpha")
    
    # UPDATE load model for residences
    for n in net:
        if n in homelist:
            # Compute load schedule
            L = Load(24,homes[n])
            L.set_objective(cost, feed, incentive[n])
            L.solve(n,grbpath)
        
            # Update load data in network
            net.nodes[n]["load"] = [v for v in L.g_opt]
            net.nodes[n]["schedule"] = {d:L.p_sch[d] for d in L.p_sch}
        else:
            net.nodes[n]["load"] = [0.0]*24
            net.nodes[n]["schedule"] = {}
    return

def powerflow(net,R):
    """
    Checks power flow solution and save dictionary of voltages.
    """
    nodelist = [n for n in net if net.nodes[n]['label'] != 'S']
    # Power consumption and node voltage over time window
    P = np.array([net.nodes[n]['load'] for n in nodelist])
    V = 1.0 - np.matmul(R,P)
    # Update the voltage data
    for i,n in enumerate(nodelist):
        net.nodes[n]["volt"] = V[i,:]
    return


def get_incentive_dual(net,R,eps=0.01,phi=1e-4,vmin=0.95,vmax=1.05):
    # Get dual variables
    nodelist = [n for n in net if net.nodes[n]['label'] != 'S']
    mu_low = np.array([net.nodes[n]["mu_low"] for n in nodelist])
    mu_up = np.array([net.nodes[n]["mu_up"] for n in nodelist])
    V = np.array([net.nodes[n]["volt"] for n in nodelist])
    
    # Update dual
    mu_low = (1-eps*phi)*mu_low + eps*(vmin - V)
    mu_up = (1-eps*phi)*mu_up + eps*(V - vmax)
    alpha = np.matmul(R,(mu_low-mu_up))
    for i,n in enumerate(nodelist):
        net.nodes[n]["alpha"] = alpha[i,:]
        net.nodes[n]["mu_low"] = mu_low[i,:]
        net.nodes[n]["mu_up"] = mu_up[i,:]
    return


def get_incentive(net):
    # Get voltages
    nodelist = [n for n in net if net.nodes[n]['label'] != 'S']
    V = np.array([net.nodes[n]["volt"] for n in nodelist])
    # A = np.array([net.nodes[n]["alpha"] for n in nodelist])
    
    # Update dual
    alpha = (V - 0.98)/(1.0-np.amin(V))
    for i,n in enumerate(nodelist):
        net.nodes[n]["alpha"] = alpha[i,:]
    return
    
#%% Cost Profile

# Cost profile taken for the off peak plan of AEP
# COST = [0.073626]*5 + [0.092313]*10 + [0.225525]*3 + [0.092313]*6
# COST = [0.093626]*5 + [0.072313]*10 + [0.225525]*3 + [0.092313]*6
COST = [1]*24

# Feed in tarriff rate for small residence owned rooftop solar installations
FEED = 0.38

#%% Network definition
dist = nx.Graph()
edgelist = [(0,10),(10,11),(11,12),(12,13),(13,14),(11,1),(1,2),(12,3),(12,4),
            (14,5),(5,6)]
dist.add_edges_from(edgelist)
nlabel = {0:'S',1:'H',2:'H',3:'H',4:'H',5:'H',6:'H',
          10:'R',11:'T',12:'T',13:'R',14:'T'}
ncord = {0:[0,0],1:[1,-1],2:[1,-2],3:[2,-1],4:[2,1],5:[4,-1],6:[4,-2],
         10:[0.5,0],11:[1,0],12:[2,0],13:[3,0],14:[4,0]}
elabel = {(0,10):'E',(10,11):'P',(11,12):'P',(12,13):'P',(13,14):'P',
          (11,1):'S',(1,2):'S',(12,3):'S',(12,4):'S',(14,5):'S',(5,6):'S'}

util = 20
prefix = "4low2highload"+"net20"+"pos0"
# prefix = "alllowload"+"net0"+"pos0"

e_r = {(0,10):1e-12, (10,11):0.001,(11,12):0.001,(12,13):0.001,(13,14):0.001*util,
          (11,1):0.0005,(1,2):0.0005,(12,3):0.0005,(12,4):0.0005,(14,5):0.0005,(5,6):0.0005}

nx.set_edge_attributes(dist, e_r, 'r')
nx.set_edge_attributes(dist, elabel, 'label')

nx.set_node_attributes(dist, ncord, 'cord')
nx.set_node_attributes(dist, nlabel, 'label')

for e in edgelist:
    dist.edges[e]['geometry'] = LineString((ncord[e[0]],ncord[e[1]]))

for n in dist:
    dist.nodes[n]['load'] = [0.0]*24
    dist.nodes[n]['alpha'] = [0.0]*24
    dist.nodes[n]["mu_low"] = [1.0]*24
    dist.nodes[n]["mu_up"] = [1.0]*24
    
R = compute_Rmat(dist)

# Home data
homeid = [51121021300494]*4+[511210207001189]*2
# homeid = [511210207001189]*6
# homeid = [51121021300494]*6
homes = {k+1:all_homes[h] for k,h in enumerate(homeid)}

#%% Iterative algorithm

alpha_history = {}
volt_history = {}
load_history = {}
sl_history = {}
iterations = 5

k = 0
while(k <= iterations):
    # update schedule
    compute_power_schedule(dist,homes,COST,FEED)
    
    # update powerflow
    powerflow(dist,R)
    
    # update incentive
    # get_incentive_dual(dist,R)
    get_incentive(dist)
    
    alpha_history[k] = nx.get_node_attributes(dist,"alpha")
    volt_history[k] = nx.get_node_attributes(dist,"volt")
    load_history[k] = nx.get_node_attributes(dist,"load")
    sl_history[k] = nx.get_node_attributes(dist,"schedule")
    k = k + 1

#Plot incentive evolution
import matplotlib.pyplot as plt

homelist = sorted([n for n in dist if dist.nodes[n]['label'] == 'H'])
xarray = np.linspace(0,25,25)


for device in ['laundry','dwasher']:
    fig = plt.figure(figsize = (20,16))
    for i,h in enumerate(homelist):
        ax = fig.add_subplot(6,1,i+1)
        if device in homes[h]["SL"]:
            # ax.step(xarray,[0]+homes[h]["ORG"][device],label="original profile")
            for m in range(iterations):
                ax.step(xarray,[0]+sl_history[m][h][device],
                        label="iter="+str(m+1))
            ax.legend(ncol=5,prop={'size': 15})
        ax.set_ylabel("Sch. Load (kW)",fontsize=15)
    fig.savefig("{}{}.png".format(figpath,prefix+'-toy-usage-'+device),
                bbox_inches='tight')

fig2 = plt.figure(figsize=(20,16))
for i,h in enumerate(homelist):
    ax2 = fig2.add_subplot(6,1,i+1)
    # ax2.step(xarray,[0]+homes[h]["ORG"]["total"],label="original profile")
    for m in range(iterations):
        ax2.step(xarray,[0]+load_history[m][h],
                 label="iter="+str(m+1))
        ax2.legend(ncol=5,prop={'size': 15})
    ax2.set_ylabel("Total load (kW)",fontsize=15)
fig2.savefig("{}{}.png".format(figpath,prefix+'-toy-usage-total'),
             bbox_inches='tight')


fig3 = plt.figure(figsize=(20,16))
for i,h in enumerate(homelist):
    ax3 = fig3.add_subplot(6,1,i+1)
    for m in range(iterations):
        ax3.step(xarray,np.insert(alpha_history[m][h],0,0),
                 label="iter="+str(m+1))
        ax3.legend(ncol=5,prop={'size': 15})
    ax3.set_ylabel("Incentive signal",fontsize=15)
fig3.savefig("{}{}.png".format(figpath,prefix+'-toy-usage-incentive'),
             bbox_inches='tight')

fig4 = plt.figure(figsize=(20,16))
for i,h in enumerate(homelist):
    ax4 = fig4.add_subplot(6,1,i+1)
    for m in range(iterations):
        ax4.step(xarray,np.insert(volt_history[m][h],0,1),
                 label="iter="+str(m+1))
        ax4.legend(ncol=5,prop={'size': 15})
    ax4.set_ylabel("Voltage (pu)",fontsize=15)
fig4.savefig("{}{}.png".format(figpath,prefix+'-toy-usage-voltage'),
             bbox_inches='tight')



