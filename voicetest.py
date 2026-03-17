import os
import json
import queue
import sounddevice as sd
from vosk import Model, KaldiRecognizer

import osmnx as ox
import networkx as nx
import pyttsx3
import geocoder


# ---------------- SPEECH OUTPUT ----------------
engine = pyttsx3.init()

def speak(text):
    print(text)
    engine.say(text)
    engine.runAndWait()


# ---------------- LOAD CAMPUS MAP ----------------
place = "Dr B R Ambedkar National Institute of Technology Jalandhar"

print("Downloading campus graph...")
graph = ox.graph_from_place(place, network_type="walk")

print("Graph ready")


# ---------------- GET CAMPUS PLACES ----------------
tags = {"building": True, "amenity": True}

places = ox.features_from_place(place, tags)
places = places.dropna(subset=["name"])

location_nodes = {}

print("Extracting campus locations...")

for idx, row in places.iterrows():

    name = row["name"]

    lat = row.geometry.centroid.y
    lon = row.geometry.centroid.x

    node = ox.distance.nearest_nodes(graph, lon, lat)

    location_nodes[name.lower()] = node

print("Total locations:", len(location_nodes))


# ---------------- CURRENT LOCATION ----------------
speak("Detecting your location")

g = geocoder.ipinfo('me')
lat=31.399996680850897
lon=75.53480550965905

start_node = ox.distance.nearest_nodes(graph, lon, lat)

print("Current location:", lat, lon)


# ---------------- LOAD VOSK MODEL ----------------
model_path = "vosk-model-small-en-in-0.4"

model = Model(model_path)

recognizer = KaldiRecognizer(model, 16000)

q = queue.Queue()


def callback(indata, frames, time, status):
    q.put(bytes(indata))


# ---------------- LISTEN FOR DESTINATION ----------------
speak("Say your destination")

destination = None

with sd.RawInputStream(
        samplerate=16000,
        blocksize=8000,
        dtype='int16',
        channels=1,
        callback=callback):

    while True:

        data = q.get()

        if recognizer.AcceptWaveform(data):

            result = json.loads(recognizer.Result())

            destination = result.get("text")

            break


print("Destination spoken:", destination)


# ---------------- MATCH DESTINATION ----------------
dest_node = None

for place in location_nodes:

    if destination in place:

        dest_node = location_nodes[place]

        print("Matched:", place)

        break


if dest_node is None:

    speak("Destination not found")

    exit()


# ---------------- CALCULATE ROUTE ----------------
speak("Calculating route")

route = nx.shortest_path(graph, start_node, dest_node, weight="length")

distance = nx.shortest_path_length(graph, start_node, dest_node, weight="length")

distance = round(distance)

print("Distance:", distance, "meters")

speak(f"Distance is {distance} meters")


# ---------------- SHOW ROUTE ----------------
ox.plot_graph_route(graph, route)


speak("Route displayed")