"""Structural rules for an entirely synthetic lake view trial."""
def potential_use(i,offered):
    return int(i%17==0 or (offered and i%4!=0))
def potential_success(i,used):
    return int(i%11<4 or (used and i%6<3))
