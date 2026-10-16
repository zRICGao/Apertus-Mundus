from entity_service import *

_HARD_MAX = 100
_PROFILE = {"trust": (0.7, 0.0), "fear": (0.3, 0.03), "irritation": (0.3, 0.08)}

def apply_emotion_delta(emo: dict, key: str, delta: int) -> dict:
    """
    Add delta to emo[key], applying damping and betrayal amplification.
    Return the modified emo dictionary (mutated in place and returned).

    Damping rules:
    - Reinforcement in the same direction (delta and current share a sign and current is far from zero): damping grows near the extremes.
      Event intensity magnitude = min(|delta|/20, 1.0) can bypass damping; major events are barely damped.
    - Reversals (delta and current have opposite signs): no damping; apply directly.
      Negative trust changes also receive betrayal amplification: deeper trust means greater betrayal damage.
    """
    damping, _ = _PROFILE.get(key, (0.5, 0.0))
    current = emo.get(key, 0)

    same_direction = (delta > 0 and current > 0) or (delta < 0 and current < 0)

    if same_direction:
        # Intensity bypass: |delta| >= 20 gives magnitude=1, fully bypassing damping
        magnitude = min(abs(delta) / 20.0, 1.0)
        saturation = abs(current) / _HARD_MAX  # 0 to 1; greater saturation means greater damping
        effective_damping = damping * saturation * (1.0 - magnitude)
        actual_delta = delta * (1.0 - effective_damping)
    else:
        # Reverse direction: no damping; additional betrayal amplification for trust
        if key == "trust" and current > 0 and delta < 0:
            # At trust=+100 factor is 3.0; at trust=+50 factor is 2.0
            betrayal_multiplier = 1.0 + 2.0 * (current / _HARD_MAX)
            actual_delta = delta * betrayal_multiplier
        else:
            actual_delta = delta

    new_val = current + actual_delta
    emo[key] = max(-_HARD_MAX, min(_HARD_MAX, round(new_val)))
    return emo


def tick_emotion_decay(emo: dict) -> dict:
    """
    Apply one natural decay step to positive fear and irritation.
    Trust never decays; negative values never decay; magnitudes under 1 become zero.
    """
    for key in ("fear", "irritation"):
        _, decay_rate = _PROFILE[key]
        val = emo.get(key, 0)
        if val > 0 and decay_rate > 0:
            val *= (1.0 - decay_rate)
            emo[key] = 0 if val < 1 else round(val)
    return emo


if __name__ == "__main__":
    # Self-test: verify five sequences
    print("=== Self-test: emotion state machine ===")

    # Sequence 1: repeated +10 from trust=0 should slow down gradually
    emo = {"trust": 0, "fear": 0, "irritation": 0}
    print("trust: +10 six times: ", end=" ")
    for _ in range(6):
        apply_emotion_delta(emo, "trust", 10)
        print(emo["trust"], end=" ")
    print()

    # Sequence 2: +20 at trust=50 (lifesaving event) should add nearly 20
    emo = {"trust": 50, "fear": 0, "irritation": 0}
    apply_emotion_delta(emo, "trust", 20)
    print(f"trust=50 + 20 (lifesaving) -> {emo['trust']}  (expected ~70)")

    # Sequence 3: -20 at trust=100 (major betrayal) should drop toward 0
    emo = {"trust": 100, "fear": 0, "irritation": 0}
    apply_emotion_delta(emo, "trust", -20)
    print(f"trust=100 + (-20) (betrayal) -> {emo['trust']}  (expected ~0)")

    # Sequence 4: 20 decay ticks from fear=80 should leave approximately 44
    emo = {"trust": 0, "fear": 80, "irritation": 0}
    for _ in range(20):
        tick_emotion_decay(emo)
    print(f"fear=80 after 20 decay ticks -> {emo['fear']}  (expected ~44)")

    # Sequence 5: irritation=80 after 10 decay ticks should be ~34; 15 ticks nearly zero
    emo = {"trust": 0, "fear": 0, "irritation": 80}
    for _ in range(10):
        tick_emotion_decay(emo)
    print(f"irritation=80 after 10 decay ticks -> {emo['irritation']}  (expected ~34)")
    for _ in range(5):
        tick_emotion_decay(emo)
    print(f"irritation=80 after 15 decay ticks -> {emo['irritation']}  (expected near zero)")
