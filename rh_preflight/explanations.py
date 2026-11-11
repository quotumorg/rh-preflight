"""Target explanations reuse policy predicates without controlling the gate."""

from dataclasses import replace

from .policy import evaluate


def target_rules(cards, settings, now):
    # Ask-every-call and unattended settings belong to the aggregate decision.
    attended = replace(settings, mode='ask_when')
    return [[{'target': card.target, 'rule': rule}
             for rule in evaluate([card], attended, unattended=False, now=now).rules]
            for card in cards]
