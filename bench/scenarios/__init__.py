"""Benchmark scenario definitions."""

from bench.scenarios.amazon_cart import AMAZON_SCENARIOS
from bench.scenarios.linkedin_vc import LINKEDIN_VC_SCENARIOS
from bench.scenarios.navigation import NAVIGATION_SCENARIOS
from bench.scenarios.phase6_features import PHASE6_SCENARIOS
from bench.scenarios.phase7_features import PHASE7_SCENARIOS
from bench.scenarios.phase8_features import PHASE8_SCENARIOS
from bench.scenarios.phase9_features import PHASE9_SCENARIOS
from bench.scenarios.phase10_features import PHASE10_SCENARIOS
from bench.scenarios.youtube_history import YOUTUBE_SCENARIOS

# Main test suite: smoke tests + primary regression scenarios (YouTube, Amazon)
ALL_SCENARIOS = [
    *NAVIGATION_SCENARIOS,
    *PHASE6_SCENARIOS,
    *PHASE7_SCENARIOS,
    *PHASE8_SCENARIOS,
    *PHASE9_SCENARIOS,
    *PHASE10_SCENARIOS,
    *YOUTUBE_SCENARIOS,
    *AMAZON_SCENARIOS,
]

# LinkedIn VC tracker is verified manually — too costly/flaky for automated suite
EXTENDED_SCENARIOS = [
    *ALL_SCENARIOS,
    *LINKEDIN_VC_SCENARIOS,
]
