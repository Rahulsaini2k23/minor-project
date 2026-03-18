import osmnx as ox
import networkx as nx

place = "Dr B R Ambedkar National Institute of Technology Jalandhar"

graph = ox.graph_from_place(place, network_type="walk")

# pick two nodes
nodes = list(graph.nodes)

start = nodes[0]
end = nodes[10]

# compute shortest path
route = nx.shortest_path(graph, start, end, weight="length")

# plot route
ox.plot_graph_route(graph, route)
print("Total nodes:", len(nodes))