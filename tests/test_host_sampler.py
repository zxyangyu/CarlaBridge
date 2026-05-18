"""Tests for host resource sampling."""

from __future__ import annotations

from carlabridge.obs.host_sampler import HostSampler


def test_host_sampler_returns_bounded_keys():
    sampler = HostSampler()
    out = sampler.sample()
    assert set(out) == {"cpu", "gpu", "mem", "net"}
    for v in out.values():
        assert isinstance(v, float)
        assert 0.0 <= v <= 100.0


def test_host_sampler_net_warmup_is_zero():
    sampler = HostSampler()
    first = sampler.sample()
    assert first["net"] == 0.0
    second = sampler.sample()
    assert 0.0 <= second["net"] <= 100.0
