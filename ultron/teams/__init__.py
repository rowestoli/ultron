from .etsy import EtsyTeam
from .pinterest import PinterestTeam

# Add a team: write a Team subclass, add it here and under `teams:` in config.yaml.
REGISTRY = {
    "etsy": EtsyTeam,
    "pinterest": PinterestTeam,
}
