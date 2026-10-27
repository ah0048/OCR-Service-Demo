"""Small supervised classifiers for printed field words (cropped text only)."""
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps
import torch
from torch import nn


def encode_image(source):
    with Image.open(source) as im:
        gray = ImageOps.autocontrast(im.convert('L'))
        gray.thumbnail((152, 40), Image.Resampling.LANCZOS)
        canvas = Image.new('L', (160, 48), 255)
        canvas.paste(gray, ((160 - gray.width) // 2, (48 - gray.height) // 2))
        return torch.from_numpy(np.asarray(canvas).copy()).float().unsqueeze(0) / 255


class WordClassifier(nn.Module):
    def __init__(self, classes):
        super().__init__()
        self.network = nn.Sequential(
            nn.Conv2d(1, 12, 3, padding=1), nn.ReLU(), nn.MaxPool2d(2),
            nn.Conv2d(12, 24, 3, padding=1), nn.ReLU(), nn.MaxPool2d(2),
            nn.Conv2d(24, 48, 3, padding=1), nn.ReLU(),
            nn.AdaptiveAvgPool2d((3, 10)), nn.Flatten(),
            nn.Linear(1440, 96), nn.ReLU(), nn.Dropout(0.25),
            nn.Linear(96, classes),
        )

    def forward(self, inputs):
        return self.network(inputs)


class PrintedFieldReader:
    def __init__(self, checkpoint):
        state = torch.load(Path(checkpoint), map_location='cpu', weights_only=True)
        self.classes = state['classes']
        self.field = state['field']
        self.model = WordClassifier(len(self.classes))
        self.model.load_state_dict(state['state_dict'])
        self.model.eval()

    @torch.inference_mode()
    def predict(self, crop, threshold=0.9):
        probabilities = self.model(encode_image(crop).unsqueeze(0)).softmax(1)[0]
        confidence, index = probabilities.max(0)
        label = self.classes[index.item()]
        return {'field': self.field, 'candidate': label,
                'text': label if confidence.item() >= threshold else None,
                'score': confidence.item(),
                'status': 'extracted' if confidence.item() >= threshold else 'needs_review',
                'note': 'Softmax score is uncalibrated; input must be a printed field crop.'}

