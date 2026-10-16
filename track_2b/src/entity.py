from entity_service import *

_HARD_MAX = 100
_PROFILE = {"trust": (0.7, 0.0), "fear": (0.3, 0.03), "irritation": (0.3, 0.08)}

def apply_emotion_delta(emo: dict, key: str, delta: int) -> dict:
    """
    将 delta 叠加到 emo[key]，应用阻尼和背叛放大。
    返回修改后的 emo（in-place 同时返回）。

    阻尼逻辑：
    - 同向堆叠（delta 与 current 同号，且 current 已离 0 较远）：越接近极值阻尼越强。
      但事件烈度 magnitude = min(|delta|/20, 1.0) 可穿透阻尼，大事件几乎不受阻尼。
    - 反向拉回（delta 与 current 异号）：无阻尼，直接入账。
      trust 反向时额外应用背叛放大：信任越深，背叛伤害越大。
    """
    damping, _ = _PROFILE.get(key, (0.5, 0.0))
    current = emo.get(key, 0)

    same_direction = (delta > 0 and current > 0) or (delta < 0 and current < 0)

    if same_direction:
        # 烈度穿透：|delta| >= 20 时 magnitude=1，完全绕过阻尼
        magnitude = min(abs(delta) / 20.0, 1.0)
        saturation = abs(current) / _HARD_MAX  # 0→1，越满越堵
        effective_damping = damping * saturation * (1.0 - magnitude)
        actual_delta = delta * (1.0 - effective_damping)
    else:
        # 反向通路：无阻尼，trust 额外背叛放大
        if key == "trust" and current > 0 and delta < 0:
            # trust=+100 时系数 3.0，trust=+50 时系数 2.0
            betrayal_multiplier = 1.0 + 2.0 * (current / _HARD_MAX)
            actual_delta = delta * betrayal_multiplier
        else:
            actual_delta = delta

    new_val = current + actual_delta
    emo[key] = max(-_HARD_MAX, min(_HARD_MAX, round(new_val)))
    return emo


def tick_emotion_decay(emo: dict) -> dict:
    """
    对 fear 和 irritation 的正值执行一次自然衰减。
    trust 永不衰减；负值永不衰减；衰减到 <1 直接归零。
    """
    for key in ("fear", "irritation"):
        _, decay_rate = _PROFILE[key]
        val = emo.get(key, 0)
        if val > 0 and decay_rate > 0:
            val *= (1.0 - decay_rate)
            emo[key] = 0 if val < 1 else round(val)
    return emo


if __name__ == "__main__":
    # 自测：验证四个序列
    print("=== 自测：情绪状态机 ===")

    # 序列1：trust 从 0 连续 +10，应逐步减速
    emo = {"trust": 0, "fear": 0, "irritation": 0}
    print("trust 连续 +10×6：", end=" ")
    for _ in range(6):
        apply_emotion_delta(emo, "trust", 10)
        print(emo["trust"], end=" ")
    print()

    # 序列2：trust=+50 收到 +20（救命级），应实入账 ≈+20
    emo = {"trust": 50, "fear": 0, "irritation": 0}
    apply_emotion_delta(emo, "trust", 20)
    print(f"trust=50 + 20(救命级) → {emo['trust']}  (期望 ≈70)")

    # 序列3：trust=+100 收到 -20（重大背叛），应从 +100 砸到 ≈0
    emo = {"trust": 100, "fear": 0, "irritation": 0}
    apply_emotion_delta(emo, "trust", -20)
    print(f"trust=100 + (-20)(背叛) → {emo['trust']}  (期望 ≈0)")

    # 序列4：fear=80 衰减 20 tick，应还剩 ≈44
    emo = {"trust": 0, "fear": 80, "irritation": 0}
    for _ in range(20):
        tick_emotion_decay(emo)
    print(f"fear=80 衰减 20 tick → {emo['fear']}  (期望 ≈44)")

    # 序列5：irritation=80 衰减 10 tick，期望 ≈34；15 tick 接近消散
    emo = {"trust": 0, "fear": 0, "irritation": 80}
    for _ in range(10):
        tick_emotion_decay(emo)
    print(f"irritation=80 衰减 10 tick → {emo['irritation']}  (期望 ≈34)")
    for _ in range(5):
        tick_emotion_decay(emo)
    print(f"irritation=80 衰减 15 tick → {emo['irritation']}  (期望接近消散)")
