from pathlib import Path
import yaml
from agent.contracts import ActionProposal
from .tiers import tier_for, permitted

class Remedy:
    def __init__(self, path=Path(__file__).parents[1] / 'config/remedies.yaml'):
        self.config = yaml.safe_load(Path(path).read_text())

    def check(self, p):
        c = self.config
        if p.action not in c['allowlist'] or p.target not in c['services']:
            raise ValueError('Action or target outside allowlist')
        if p.params or p.tier != tier_for(p.action, p.target, c['stateful']):
            raise ValueError('Unsupported parameters or incorrect tier')
        if p.action == 'throttle_load' and p.target != 'load-generator':
            raise ValueError('Throttle target must be load-generator')

    def propose(self, hypothesis, diagnosis):
        h, c = hypothesis, self.config
        if diagnosis.state != 'CONFIDENT' or diagnosis.selected_id != h.id:
            return None
        selected = [item for item in diagnosis.hypotheses if item.id == h.id]
        if len(selected) != 1 or selected[0] != h:
            return None
        action = c['remedies'].get(h.cause)
        if not action:
            return None
        tier = tier_for(action, h.component, c['stateful'])
        if not permitted(tier, h.confidence):
            return None
        p = ActionProposal(action=action, target=h.component, tier=tier,
                           reason=h.cause, hypothesis_id=h.id)
        try:
            self.check(p)
        except ValueError:
            return None
        return p

propose = Remedy().propose
