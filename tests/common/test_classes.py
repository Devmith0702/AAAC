from aaac.common.classes import AccessClass, downgrade, variant_for

def test_downgrade():
    assert downgrade(AccessClass.HIGH) == AccessClass.MEDIUM
    assert downgrade(AccessClass.MEDIUM) == AccessClass.LOW
    assert downgrade(AccessClass.LOW) == AccessClass.LOW

def test_variant_for():
    assert variant_for(AccessClass.HIGH) == "full"
    assert variant_for(AccessClass.MEDIUM) == "reduced"
    assert variant_for(AccessClass.LOW) == "essential"
