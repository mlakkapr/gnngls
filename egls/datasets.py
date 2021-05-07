import os
backend = 'pytorch'
os.environ['DGLBACKEND'] = backend

import torch
import torch.utils.data
import dgl
import numpy as np
import networkx as nx
import pickle
import pathlib

from operator import itemgetter

from . import algorithms, tour_cost, tour_to_edge_attribute, fixed_edge_tour, optimal_cost as get_optimal_cost

def _get_from_edge_dict(d, k):
    return d[k] if k in d else d[tuple(reversed(k))]

def set_features(G, depot):
    # nearest neighbour solution
    nn_solution = algorithms.nearest_neighbor(G, 0, weight='weight')
    in_nn_solution = tour_to_edge_attribute(G, nn_solution)

    # farthest insertion solution
    fi_solution = algorithms.insertion(G, 0, mode='farthest', weight='weight')
    in_fi_solution = tour_to_edge_attribute(G, fi_solution)

    # nearest insertion solution
    ni_solution = algorithms.insertion(G, 0, mode='nearest', weight='weight')
    in_ni_solution = tour_to_edge_attribute(G, ni_solution)

    # remove longest edges until minimum degree is reached
    min_degree_graph = get_min_degree_graph(G, 2, weight='weight')

    # minimum spanning tree
    mst = get_mst(G, weight='weight')

    # betweenness centrality
    betweenness = nx.edge_betweenness_centrality(G, weight='weight')

    # random walk betweenness centrality
    rw_betweenness = nx.edge_current_flow_betweenness_centrality(G, weight='weight')

    # neighbours, ordered
    nn = get_nearest_neighbours(G, weight='weight')

    # distance to depot
    depot_weight = get_depot_weight(G, 0, weight='weight')

    # width accordig to KGLS
    width = get_width(G, 0)

    # closeness centrality
    closeness = nx.closeness_centrality(G, distance='weight')

    # random walk closeness centrality
    rw_closeness = nx.current_flow_closeness_centrality(G, weight='weight')

    # clustering coefficient
    clustering = nx.clustering(G, weight='weight')


    for e in G.edges:
        i, j = e

        G.edges[e]['features'] = np.array([
            G.edges[e]['weight'],
            np.abs(width[i] - width[j]),
            nn[i][j],
            nn[j][i],
            nn[i][j] == nn[j][i],
            nn[i][j] <= 0.1*len(G.nodes) or nn[j][i] <= 0.1*len(G.nodes),
            nn[i][j] <= 0.2*len(G.nodes) or nn[j][i] <= 0.2*len(G.nodes),
            nn[i][j] <= 0.3*len(G.nodes) or nn[j][i] <= 0.3*len(G.nodes),
            _get_from_edge_dict(in_nn_solution, e),
            _get_from_edge_dict(in_fi_solution, e),
            _get_from_edge_dict(in_ni_solution, e),
            _get_from_edge_dict(min_degree_graph, e),
            _get_from_edge_dict(mst, e),
            _get_from_edge_dict(betweenness, e),
            _get_from_edge_dict(rw_betweenness, e),
        ], dtype=np.float32)

    for n in G.nodes:
        G.nodes[n]['features'] = np.array([
            width[n],
            depot_weight[n],
            closeness[n],
            rw_closeness[n],
            clustering[n],
        ], dtype=np.float32)

def set_labels(G, depot):
    optimal_cost = get_optimal_cost(G)
    regret = get_regret(G, optimal_cost)

    for e in G.edges:
        G.edges[e]['regret'] = regret[e].astype(float32)

def get_regret(G, optimal_cost):
    regret = {}

    for e in G.edges:
        if G.edges[e]['in_solution']:
            regret[e] = 0.
        else:
            fixed_edge_tour = fixed_edge_tour(G, e, scale=1e6, max_trials=100, runs=10)
            fixed_edge_cost = tour_cost(G, fixed_edge_tour)
            regret[e] = (fixed_edge_cost - optimal_cost)/optimal_cost

    return regret

def get_width(G, depot):
    pos = []
    n2i = {}
    for i, n in enumerate(G.nodes):
        pos.append(G.nodes[n]['pos'])
        n2i[n] = i

    pos = np.vstack(pos)

    center = pos.mean(axis=0)
    center_line = center - pos[n2i[depot]]

    u = center_line/np.linalg.norm(center_line)
    n = np.array([-u[1], u[0]])

    v = pos - pos[n2i[depot]]

    width = np.apply_along_axis(np.dot, 1, v, n)

    return {n: width[i] for n, i in n2i.items()}


def get_nearest_neighbours(G, weight='weight'):
    neighbours_ranked = {}

    for i in G.nodes:
        neighbours = [(j, G.edges[(i, j)][weight]) for j in G.neighbors(i)]
        neighbours_sorted = sorted(neighbours, key=itemgetter(1))
        neighbours_ranked[i] = {j: k for k, (j, _) in enumerate(neighbours_sorted)}

    return neighbours_ranked

def get_min_degree_graph(G, min_degree, weight='weight'):
    edges = sorted([(e, G.edges[e][weight]) for e in G.edges], key=itemgetter(1), reverse=True)
    edges, _ = zip(*edges)
    edges = list(edges)

    H = G.edge_subgraph(edges)
    while min(dict(nx.degree(H)).values()) > min_degree:
        edges.pop(0)
        H = G.edge_subgraph(edges)

    return {e: e in edges for e in G.edges}

def get_depot_weight(G, depot, weight='weight'):
    depot_weight = {}
    for n in G.nodes:
        if n == depot:
            depot_weight[n] = 0
        else:
            depot_weight[n] = G.edges[(depot, n)][weight]
    return depot_weight

def get_mst(G, weight='weight'):
    mst = {e: False for e in G.edges}
    mst_edges = nx.minimum_spanning_edges(G, weight=weight, algorithm='kruskal', data=False)
    for e in mst_edges:
        if e in mst:
            mst[e] = True
    assert sum(mst.values()) == len(G.nodes) - 1
    return mst


class TSPDataset(torch.utils.data.Dataset):
    def __init__(self, instances_file, scalers_file=None):
        if not isinstance(instances_file, pathlib.Path):
            instances_file = pathlib.Path(instances_file)

        self.instances = [line.strip() for line in open(instances_file)]
        self.root_dir = instances_file.parent
        if scalers_file is None:
            scalers_file = self.root_dir / 'scalers.pkl'
        self.scalers = pickle.load(open(scalers_file, 'rb'))

    def __len__(self):
        return len(self.instances)

    def __getitem__(self, i):
        if torch.is_tensor(i):
            i = i.tolist()

        G = nx.read_gpickle(self.root_dir / self.instances[i])
        H = self.nx_to_dgl(G)
        return H

    def nx_to_dgl(self, G, index_edges=False):
        e2i = {}
        regret = []
        efeats = []
        for i, e in enumerate(G.edges):
            e2i[e] = i
            regret.append(G.edges[e]['regret'])
            efeats.append(G.edges[e]['features'])
        regret = self.scalers['edges']['regret'].transform(np.vstack(regret)).astype(np.float32)
        efeats = self.scalers['edges']['features'].transform(np.vstack(efeats))

        n2i = {}
        nfeats = []
        for i, n in enumerate(G.nodes):
            n2i[n] = i
            nfeats.append(G.nodes[n]['features'])
        nfeats = self.scalers['nodes']['features'].transform(np.vstack(nfeats))

        lG = nx.line_graph(G)
        for n in lG.nodes:
            i, j = n
            lG.nodes[n]['in_solution'] = np.array([G.edges[n]['in_solution']])
            lG.nodes[n]['regret'] = regret[e2i[n]]
            lG.nodes[n]['features'] = np.hstack((
                nfeats[n2i[i]],
                efeats[e2i[n]],
                nfeats[n2i[j]]
            ))
            if index_edges:
                lG.nodes[n]['e'] = n

        attrs = ['features', 'regret', 'in_solution']
        if index_edges:
            attrs.append('e')
        H = dgl.from_networkx(lG, node_attrs=attrs)
        return H
