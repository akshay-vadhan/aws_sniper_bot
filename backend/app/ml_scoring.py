from dataclasses import dataclass
import math


FEATURES = ("win_rate", "trade_count", "realized_pnl_pct")


def transform(values: dict[str, float]) -> list[float]:
    return [max(0.0, min(1.0, values.get("win_rate", 0.0))),
            min(1.0, math.log1p(max(0.0, values.get("trade_count", 0.0))) / math.log(101)),
            math.tanh(values.get("realized_pnl_pct", 0.0) / 100.0)]


@dataclass
class LogisticWalletModel:
    weights: list[float]
    bias: float
    examples: int

    def predict(self, features: dict[str, float]) -> float:
        z = self.bias + sum(w * x for w, x in zip(self.weights, transform(features)))
        return 1 / (1 + math.exp(-max(-30, min(30, z))))


def train_logistic(examples: list[dict], epochs: int = 700, learning_rate: float = 0.15,
                   l2: float = 0.01) -> LogisticWalletModel:
    if len(examples) < 20 or {int(row["profitable"]) for row in examples} != {0, 1}:
        raise ValueError("Training needs at least 20 labeled examples and both outcome classes")
    xs = [transform(row["features"]) for row in examples]
    ys = [int(row["profitable"]) for row in examples]
    weights = [0.0] * len(FEATURES)
    bias = 0.0
    for _ in range(epochs):
        grad_w = [0.0] * len(weights)
        grad_b = 0.0
        for x, y in zip(xs, ys):
            z = bias + sum(w * value for w, value in zip(weights, x))
            pred = 1 / (1 + math.exp(-max(-30, min(30, z))))
            error = pred - y
            grad_b += error
            for i, value in enumerate(x):
                grad_w[i] += error * value
        scale = 1 / len(xs)
        bias -= learning_rate * grad_b * scale
        weights = [w - learning_rate * (grad * scale + l2 * w) for w, grad in zip(weights, grad_w)]
    return LogisticWalletModel(weights, bias, len(examples))
