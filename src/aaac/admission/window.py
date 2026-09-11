from __future__ import annotations
from aaac.common.classes import AccessClass
from aaac.common.config import AdmissionConfig


def window_for(access_class: AccessClass, cfg: AdmissionConfig, mode: str) -> float:
    """Calculate the admission window (C2) for the given access class and run mode.

    The admission window determines how long a ticket has to complete its
    interaction with the origin before it is considered timed-out. This is
    the core mechanism of C2 (Adaptive Admission Window):

    - **aaac mode**: W(c) = min(w_base_s * kappa[c], w_max_s)
      HIGH-class clients get a shorter window (kappa=1.0) because they have
      fast links and should complete quickly. LOW-class clients get a longer
      window (kappa=2.5) because their links are slower — giving them a fair
      chance to complete with a smaller (essential) payload.

    - **baseline mode**: W = w_base_s for all classes, regardless of link quality.
      This is the access-blind control condition for the evaluation.

    - **none mode**: No queue exists, so window is irrelevant. Returns w_base_s
      as a no-op default (this function should never be called in none mode,
      but we handle it defensively).

    The window is enforced in two places:
    1. As `exp` inside the HMAC admit token (delivery service refuses expired tokens)
    2. As the score in the `inflight` ZSET (the sweeper task expires stale tickets)
    """
    if mode != "aaac":
        # baseline and none modes: flat window, no class differentiation
        return float(cfg.w_base_s)

    # aaac mode: class-specific window via kappa multiplier
    class_name = access_class.name  # "HIGH", "MEDIUM", or "LOW"
    kappa = cfg.kappa.get(class_name, 1.0)

    return min(cfg.w_base_s * kappa, cfg.w_max_s)


def windows_for_all(cfg: AdmissionConfig, mode: str) -> dict[AccessClass, float]:
    """Return the admission window for every AccessClass as a dict.

    This is the format expected by QueueStore.admit_n()'s `windows_s` parameter.
    Computing all three upfront avoids per-ticket lookups inside the hot admission path.
    """
    return {cls: window_for(cls, cfg, mode) for cls in AccessClass}


def weighted_mean_window(
    waiting_counts: dict[AccessClass, int],
    cfg: AdmissionConfig,
    mode: str,
) -> float:
    """Compute the load-weighted mean window over currently waiting classes.

    Used by the controller (C5) to calculate C_max = ceil(mu_hat * W_mean).

    If no tickets are waiting, returns w_base_s as a safe default so the
    concurrency cap calculation doesn't divide by zero or produce NaN.
    """
    total_waiting = sum(waiting_counts.values())
    if total_waiting == 0:
        return float(cfg.w_base_s)

    weighted_sum = sum(
        count * window_for(cls, cfg, mode)
        for cls, count in waiting_counts.items()
    )
    return weighted_sum / total_waiting
