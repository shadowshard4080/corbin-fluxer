"""Tiny assertion harness. No pytest, matching the parent repo's convention."""
from __future__ import annotations
import datetime as dt, sys, traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from corbin.core.context import Member, Role, User          # noqa: E402
from corbin.core.permissions import Perm                    # noqa: E402


class Results:
    def __init__(self) -> None:
        self.passed = 0
        self.failures: list[str] = []

    def check(self, label: str, actual, expected) -> None:
        if actual == expected:
            self.passed += 1
        else:
            self.failures.append(f"{label}\n     expected: {expected!r}\n     actual:   {actual!r}")

    def ok(self, label: str, condition: bool) -> None:
        self.check(label, bool(condition), True)

    def raises(self, label: str, exc, fn, *a, **kw) -> None:
        try:
            fn(*a, **kw)
        except exc:
            self.passed += 1
        except Exception as e:  # pragma: no cover
            self.failures.append(f"{label}: wrong exception {type(e).__name__}: {e}")
        else:
            self.failures.append(f"{label}: expected {exc.__name__}, nothing raised")


def now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def member(uid: str, *, perms: Perm = Perm.NONE, position: int = 1,
           owner: bool = False, tenure_days: float = 100,
           account_days: float = 500, bot: bool = False) -> Member:
    return Member(
        user=User(id=uid, name=uid.title(), is_bot=bot,
                  created_at=now() - dt.timedelta(days=account_days)),
        guild_id="g1",
        roles=(Role(id=f"r-{uid}", name="role", permissions=int(perms),
                    position=position),),
        joined_at=now() - dt.timedelta(days=tenure_days),
        is_guild_owner=owner,
    )


def run(suites) -> int:
    total_pass, total_fail = 0, 0
    for name, fn in suites:
        r = Results()
        try:
            fn(r)
        except Exception:
            r.failures.append("SUITE CRASHED:\n" + traceback.format_exc())
        total_pass += r.passed
        total_fail += len(r.failures)
        status = "ok" if not r.failures else "FAIL"
        print(f"  {name:<34} {r.passed:>3} checks  [{status}]")
        for f in r.failures:
            print(f"      x {f}")
    print(f"\n  {total_pass} passed, {total_fail} failed")
    return 1 if total_fail else 0
