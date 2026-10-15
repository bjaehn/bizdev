from bd.scanners.base import AcceptedSignal, Rejection, ScanOutcome, Scanner, store_signals
from bd.scanners.hiring import HiringScanner
from bd.scanners.leadership import LeadershipScanner
from bd.scanners.pe import PeScanner
from bd.scanners.tech_procurement import TechProcurementScanner

# events lands in Stage 3.
SCANNERS: dict[str, type[Scanner]] = {
    c.name: c for c in (LeadershipScanner, PeScanner, HiringScanner, TechProcurementScanner)
}

__all__ = ["AcceptedSignal", "Rejection", "ScanOutcome", "Scanner", "store_signals", "SCANNERS",
           "LeadershipScanner"]
