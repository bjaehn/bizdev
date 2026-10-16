from bd.scanners.base import AcceptedSignal, Rejection, ScanOutcome, Scanner, store_signals
from bd.scanners.earnings import EarningsScanner
from bd.scanners.edgar import EdgarScanner
from bd.scanners.events import EventsScanner
from bd.scanners.hiring import HiringScanner
from bd.scanners.leadership import LeadershipScanner
from bd.scanners.pe import PeScanner
from bd.scanners.press import PressScanner
from bd.scanners.tech_procurement import TechProcurementScanner

SCANNERS: dict[str, type[Scanner]] = {
    c.name: c for c in (LeadershipScanner, EdgarScanner, EarningsScanner, EventsScanner, PressScanner, PeScanner, HiringScanner,
                      TechProcurementScanner)
}

__all__ = ["AcceptedSignal", "Rejection", "ScanOutcome", "Scanner", "store_signals", "SCANNERS",
           "LeadershipScanner", "EventsScanner", "EdgarScanner"]
