import json
from typing import List, Dict, Any
from sklearn.feature_extraction.text import TfidfVectorizer
import torch

import math
from typing import Tuple


class VectorDB:
    def __init__(self, data_file: str):
        with open(data_file, "r") as f:
            self.data = json.load(f)
        self.text_list = [
            item["title"] + " " + item["description"] for item in self.data
        ]
        self.vectorizer = TfidfVectorizer()
        self.vectors = torch.tensor(
            self.vectorizer.fit_transform(self.text_list).toarray()
        )
        self.coordinates = [
            self._parse_coordinates(item["coordinates"]) for item in self.data
        ]

    def _parse_coordinates(self, coord_str: str) -> Tuple[float, float]:
        lon, lat, _ = map(float, coord_str.split(","))
        return lat, lon

    def search(self, query: str, top_k: int = 5) -> List[Dict[str, Any]]:
        query_vector = torch.tensor(self.vectorizer.transform([query]).toarray())
        similarities = torch.cosine_similarity(query_vector, self.vectors)
        top_indices = similarities.argsort(descending=True)[:top_k]
        return [self.data[i] for i in top_indices]

    def keyword_search(self, keyword: str, top_k: int = 5) -> List[Dict[str, Any]]:
        results = []
        for item in self.data:
            if (
                keyword.lower() in item["title"].lower()
                or keyword.lower() in item["description"].lower()
            ):
                results.append(item)
        return results[:top_k]

    def find_closest(
        self, lat: float, lon: float, top_k: int = 5
    ) -> List[Dict[str, Any]]:
        distances = [
            self._haversine_distance(lat, lon, coord[0], coord[1])
            for coord in self.coordinates
        ]
        top_indices = sorted(range(len(distances)), key=lambda i: distances[i])[:top_k]
        return [self.data[i] for i in top_indices]

    def _haversine_distance(
        self, lat1: float, lon1: float, lat2: float, lon2: float
    ) -> float:
        R = 6371  # Earth's radius in kilometers

        phi1 = math.radians(lat1)
        phi2 = math.radians(lat2)
        delta_phi = math.radians(lat2 - lat1)
        delta_lambda = math.radians(lon2 - lon1)

        a = (
            math.sin(delta_phi / 2) ** 2
            + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2) ** 2
        )
        c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

        return R * c
