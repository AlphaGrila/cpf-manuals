"""
CPF vehicle scope (Steve, 25 Nov 2026). Three layers:
  1. Classics: every make and model up to a ROLLING cut-off: 25 years back (2001 in 2026),
     Japanese makes 16 years back (2010 in 2026). Each January another model year joins.
  2. Hot rod / restomod: Big Three performance models of all years (Corvette, Camaro, Mustang,
     Challenger, Viper...) plus swap engines, transmissions and systems (LS, LT, Coyote, Hemi, EFI...).
  3. Modern collectibles: exotic makes of all years (Ferrari, Lamborghini, McLaren...) and named
     halo models of everyday makes (Porsche 911, BMW M3, AMG GT, GT-R...).
The lists live in the database table vehicle_scope so they can be changed without code.
"""
import datetime
import re

JAPAN = {"Japan"}


def year_cutoff(country=None, today=None):
    y = (today or datetime.date.today()).year
    return y - (16 if country in JAPAN else 25)


def _k(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


class Scope:
    def __init__(self, rows):
        self.exotic = {_k(r["brand_name"]) for r in rows if r["kind"] == "exotic_make" and r.get("active", True)}
        self.models = [(_k(r["brand_name"]), _k(r["model_name"])) for r in rows
                       if r["kind"] in ("performance_model", "halo_model") and r.get("active", True) and r.get("model_name")]
        self.swap = [r for r in rows if r["kind"].startswith("swap_") and r.get("active", True)]

    def no_cutoff(self, brand_name, model_name=None):
        """True when this make (exotic) or this model (performance / halo) is collected for all years."""
        b = _k(brand_name)
        if b in self.exotic:
            return True
        m = _k(model_name)
        return bool(m) and any(b == sb and m.startswith(sm) for sb, sm in self.models)

    def cutoff(self, brand, model_name=None):
        """Newest model year collected for this make / model (9999 = no limit)."""
        if self.no_cutoff(brand.get("name"), model_name):
            return 9999
        return year_cutoff(brand.get("country_of_origin"))


def load(db):
    try:
        rows = db.select("vehicle_scope", "kind,brand_name,model_name,family,active", limit=5000)
    except Exception:
        rows = []
    return Scope(rows)
