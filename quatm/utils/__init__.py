"""Shared utilities used by the Quatm core."""

# Licensing and activation are entry-point concerns in the optional platform;
# the public core does not import them at package-import time.
from .paths import (PROJECT_ROOT, CONFIG_DIR, EXPERIMENTS_DIR, DATA_DIR,
                    IMAGES_DIR, QUATM_DIR, resolve_experiment_path)
