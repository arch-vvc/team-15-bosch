"""Safety floors agreed in Part 2; shared calibration belongs to Person B."""
def tier_for(action, target, stateful):
    if action == 'restart_service':
        return 2 if target in stateful else 1
    return {'throttle_load': 1, 'scale_service': 2}.get(action, 3)


def permitted(tier, confidence):
    return tier in (1, 2) and confidence >= {1: .7, 2: .85}[tier]
