from __future__ import annotations
from aaac.common.classes import AccessClass
from aaac.common.config import AdmissionConfig

def window_for(access_class: AccessClass, cfg: AdmissionConfig, mode: str) -> float:
    """Calculate the admission window (C2) for the given class and mode."""
    if mode == "baseline":
        return float(cfg.w_base_s)
        
    class_name = access_class.name
    kappa = cfg.kappa.get(class_name, 1.0)
    
    return min(cfg.w_base_s * kappa, cfg.w_max_s)
