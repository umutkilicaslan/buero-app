"""Upcoming tax deadlines. Dates falling on a weekend move to the next Monday (§ 108 Abs. 3 AO);
public holidays are not considered."""
import calendar
from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy.orm import Session

from . import services


@dataclass
class Deadline:
    due: date
    title: str
    detail: str

    def days_left(self, today: date) -> int:
        return (self.due - today).days


def _workday(d: date) -> date:
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def _add_months(d: date, n: int) -> date:
    m = d.month - 1 + n
    y = d.year + m // 12
    m = m % 12 + 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def upcoming(s: Session, today: date | None = None, horizon_days: int = 75) -> list[Deadline]:
    today = today or date.today()
    st = services.get_settings(s)
    out: list[Deadline] = []
    for year in (today.year - 1, today.year, today.year + 1):
        ku = services.is_kleinunternehmer(s, year)
        # Income tax prepayments
        for m in (3, 6, 9, 12):
            out.append(Deadline(_workday(date(year, m, 10)), "Einkommensteuer-Vorauszahlung",
                                f"Quarterly prepayment {year} (only if the Finanzamt set one)."))
        # Annual returns for the previous year: 31 July (without Steuerberater)
        out.append(Deadline(_workday(date(year, 7, 31)), f"Steuererklärungen {year - 1}",
                            f"Einkommensteuer (with Anlage EÜR){'' if services.is_kleinunternehmer(s, year - 1) else ' and Umsatzsteuer'} for {year - 1}."))
        if not ku:
            step = 1 if st.ustva_period == "monthly" else 3
            for m in range(step, 13, step):
                period_end = date(year, m, calendar.monthrange(year, m)[1])
                due = period_end + timedelta(days=10)
                if st.dauerfristverlaengerung:
                    due = _add_months(due, 1)
                label = f"{year}-{m:02d}" if step == 1 else f"Q{m // 3} {year}"
                out.append(Deadline(_workday(due), "Umsatzsteuer-Voranmeldung", f"UStVA {label} in Mein ELSTER."))
        # ZM: 25th day after each quarter in which EU reverse-charge services were invoiced
        from . import ledger
        for _, start, end in ledger.periods(year, "quarterly"):
            if ledger.zm(s, start, end):
                out.append(Deadline(_workday(_add_months(end.replace(day=25), 1)), "Zusammenfassende Meldung",
                                    f"EU reverse-charge invoices {start:%m}/{end:%m.%Y} to the BZSt (via Mein ELSTER)."))
    return sorted([d for d in out if today <= d.due <= today + timedelta(days=horizon_days)], key=lambda d: d.due)
