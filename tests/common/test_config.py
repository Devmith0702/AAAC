import pytest
from aaac.common.config import _load_config_from_file

def test_load_golden_yaml(tmp_path):
    # This expects configs/run.yaml to exist, but if run from a different dir it might fail.
    # To be safe, we'll write a temporary valid yaml and test that.
    yaml_content = """
run_id: r07
mode: aaac
seed: 1

admission:
  w_base_s: 20.0
  kappa: {HIGH: 1.0, MEDIUM: 1.5, LOW: 2.5}
  w_max_s: 60.0
  alpha_min: 5.0
  alpha_max: 400.0
  alpha_increase: 2.0
  alpha_decrease: 0.7
  control_tick_s: 1.0
  target_origin_p95_ms: 400
  target_origin_err_rate: 0.005
  max_attempts: 5
  poll_interval_ms: 2000

estimator:
  probe_bytes: 65536
  min_rtt_samples: 5
  confidence_threshold: 0.60
  model_path: models/link_classifier.joblib

delivery:
  budgets_bytes: {full: 460800, reduced: 61440, essential: 6144}

origin:
  service_time_ms: {dist: lognormal, median: 60, sigma: 0.5}
  concurrency_limit: 64
  queue_limit: 256

load:
  n_clients: 20000
  scale_factor: 10
  burst_center_s: 30
  burst_sigma_s: 15
  tail_decay_s: 600
  class_mix: {HIGH: 0.25, MEDIUM: 0.40, LOW: 0.35}
  abandon_after_s: 900
    """
    p = tmp_path / "run.yaml"
    p.write_text(yaml_content)
    
    config = _load_config_from_file(str(p))
    assert config.run_id == "r07"
    assert config.mode == "aaac"
    assert config.admission.w_base_s == 20.0
    assert config.admission.kappa["HIGH"] == 1.0
    assert config.estimator.probe_bytes == 65536
    assert config.delivery.budgets_bytes["full"] == 460800
    assert config.load.n_clients == 20000

def test_rejects_unknown_keys(tmp_path):
    yaml_content = """
run_id: r07
mode: aaac
seed: 1
unknown_root_key: 42
admission: {}
estimator: {}
delivery: {}
origin: {}
load: {}
    """
    p = tmp_path / "bad.yaml"
    p.write_text(yaml_content)
    
    with pytest.raises(ValueError, match="Unknown config keys in root: {'unknown_root_key'}"):
        _load_config_from_file(str(p))
