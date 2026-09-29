"""客观对比单元测试: verdict 纯函数全路径 (无需 SolidWorks)。"""
import pytest

from scripts.sw_compare import compare_fingerprints, collect_model_fingerprint

from fakes.sw import new_fake_session


def _fingerprint(volume=1000.0, faces=10, edges=20, features=3, components=None):
    return {
        "title": "box.SLDPRT",
        "topology": {
            "bodies": [{"faces": faces, "edges": edges}],
            "feature_count": features,
            "component_names": components or [],
        },
        "metrics": {
            "volume_mm3": volume,
            "surface_mm2": 600.0,
            "center_of_mass_mm": [0.0, 0.0, 5.0],
        },
    }


def test_identical_fingerprints_verified():
    result = compare_fingerprints(_fingerprint(), _fingerprint())
    assert result["verdict"] == "verified"
    assert result["checks"]["metrics"]["volume_rel_delta"] == 0.0


def test_volume_delta_72_percent_mismatch():
    # 10mm -> 12mm 盒: 体积 x1.2, 相对差 1/6 ≈ 16.7% (超差即 mismatch)。
    result = compare_fingerprints(_fingerprint(volume=1000.0), _fingerprint(volume=1200.0))
    assert result["verdict"] == "mismatch"
    assert abs(result["checks"]["metrics"]["volume_rel_delta"] - 1 / 6) < 0.001


def test_topology_diff_with_ok_metrics_review_required():
    # 体积/面积/质心全过但特征数不同 -> 交给人工审查而非直接判错。
    result = compare_fingerprints(_fingerprint(features=3), _fingerprint(features=5))
    assert result["verdict"] == "review_required"


def test_component_list_diff_detected():
    result = compare_fingerprints(
        _fingerprint(components=["box-1"]),
        _fingerprint(components=["box-1", "box-2"]),
    )
    assert result["verdict"] == "review_required"
    assert result["checks"]["topology"]["components_equal"] is False


def test_missing_metrics_falls_back_to_topology():
    no_metrics = _fingerprint()
    no_metrics["metrics"] = {key: None for key in no_metrics["metrics"]}
    result = compare_fingerprints(no_metrics, no_metrics)
    assert result["verdict"] == "verified"
    assert result["checks"]["metrics_available"] is False


def test_collect_model_fingerprint_on_fake_model():
    app, _journal = new_fake_session()
    model = app.NewDocument("C:/fake/templates/gb_part.prtdot", 0, 0, 0)
    fingerprint = collect_model_fingerprint(model)
    assert "topology" in fingerprint and "metrics" in fingerprint
    assert fingerprint["topology"]["component_names"] == []
