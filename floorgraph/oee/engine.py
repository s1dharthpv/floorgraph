"""Per-workstation, per-day OEE (Overall Equipment Effectiveness).

Availability = Run Minutes / Planned Minutes
    Planned Minutes comes from `Workstation.total_working_hours` - a real,
    independently-configured shift calendar (`Workstation.working_hours`) -
    NOT derived from the same Downtime Log / Job Card Time Log activity
    that defines Run Minutes. Deriving it from activity would make
    Availability tautologically ~100% (planned time would always equal
    run time + downtime, by construction). If a Workstation has no
    working_hours configured, Availability is reported as 0, not 100 -
    an unconfigured shift calendar is a data gap, not evidence of perfect
    availability. Same for a holiday (per Workstation.holiday_list,
    honoring Manufacturing Settings.allow_production_on_holidays): Planned
    Minutes is 0, not a full shift's worth - otherwise a day the plant was
    closed, with nothing to log downtime against, reported 100%.

Performance = Ideal Run Minutes / Run Minutes
    Ideal Run Minutes = sum(ideal_cycle_time_per_unit * completed_qty)
    across Job Card Time Log rows for that workstation/day.
    ideal_cycle_time_per_unit comes from the Work Order Operation's
    time_in_mins / Work Order qty - real scheduling data already produced
    by ERPNext, not invented for this calculation.

Quality = Good Qty / Attempted Qty
    Same definition on ERPNext v15 and v16, from the same signal: the Job
    Card's computed `process_loss_qty` (for_quantity - total_completed_qty,
    minus pending_qty on v16) - units that were NEVER completed, a
    yield-loss concept. Those units are, by construction, absent from
    Actual Qty (the Job Card Time Log completed_qty sum), so
    Attempted Qty = Actual Qty + Process Loss Qty and Good Qty = Actual Qty.

    v15's `Job Card Scrap Item` table is deliberately NOT read. Its rows
    are scrap items with their own item and UOM, not a count of defective
    units of the product, so summing them against completed units assumed
    a unit match that ERPNext does not guarantee. (v16 removed the table.)
    Before this change v15 used it and v16 used process_loss_qty, so the
    same shop-floor facts gave different Quality on the two versions.

    Caveat, unchanged from the earlier v16 behaviour: process_loss_qty is
    computed on every save of a partly completed Job Card, so a card still
    in progress (completed < for_quantity, nothing marked pending) counts
    the remainder as loss until it is finished or the rest is marked
    pending.

    Process loss has no per-day granularity of its own (it is a snapshot
    on the Job Card, not per-log-entry) - a job card's loss is attributed to
    the calendar date of its LAST time log that actually has
    completed_qty > 0 (not simply its last time log - a trailing zero-qty
    idle/travel entry must not "steal" the attribution date, since that day
    never has the job card in job_cards_seen and the loss would silently
    vanish rather than just being misattributed). That's a documented
    approximation for jobs spanning multiple days, not an exact daily
    slice. Loss qty and completed qty may also carry different UOMs in
    principle; no conversion is applied here.

OEE = Availability * Performance * Quality (all as 0-100 percentages,
scaled back down once).
"""

import frappe
from frappe.query_builder.functions import Sum
from frappe.utils import add_days, flt, get_datetime, getdate, today

from floorgraph.utils import is_workstation_holiday


def compute_workstation_oee(workstation, log_date):
	log_date = getdate(log_date)
	day_start = get_datetime(f"{log_date} 00:00:00")
	day_end = get_datetime(f"{log_date} 23:59:59")

	total_working_hours, holiday_list = frappe.db.get_value(
		"Workstation", workstation, ["total_working_hours", "holiday_list"]
	)
	# A holiday isn't a planned working day at all - without this, planned_minutes
	# stayed a full shift's worth even on a day the plant was closed, so zero
	# logged downtime (there was nothing to log) reported 100% Availability for
	# a day nothing ran. Same convention as an unconfigured shift calendar
	# below: 0 planned minutes, not a default of "fully available."
	planned_minutes = 0.0 if is_workstation_holiday(holiday_list, log_date) else flt(total_working_hours) * 60
	unplanned_downtime_minutes = _unplanned_downtime_minutes(workstation, day_start, day_end)

	run_minutes = max(planned_minutes - unplanned_downtime_minutes, 0)
	availability = (run_minutes / planned_minutes * 100) if planned_minutes else 0

	time_logs = _time_logs_for_day(workstation, day_start, day_end)
	actual_qty = sum(flt(row.completed_qty) for row in time_logs)

	ideal_run_minutes = 0.0
	job_cards_seen = set()
	for row in time_logs:
		if not row.completed_qty:
			continue
		ideal_run_minutes += ideal_cycle_time_per_unit(row.job_card) * flt(row.completed_qty)
		job_cards_seen.add(row.job_card)

	performance = (ideal_run_minutes / run_minutes * 100) if run_minutes else 0

	# One definition on every supported ERPNext version: process loss is
	# units that were never completed, so it is outside actual_qty.
	scrap_qty = sum(
		_job_card_process_loss_qty(job_card)
		for job_card in job_cards_seen
		if _last_time_log_date(job_card) == log_date
	)
	attempted_qty = actual_qty + scrap_qty
	good_qty = actual_qty
	quality = (good_qty / attempted_qty * 100) if attempted_qty else 0

	oee = availability * performance * quality / 10000

	return {
		"workstation": workstation,
		"log_date": log_date,
		"planned_minutes": planned_minutes,
		"unplanned_downtime_minutes": unplanned_downtime_minutes,
		"run_minutes": run_minutes,
		"availability": availability,
		"actual_qty": actual_qty,
		"ideal_run_minutes": ideal_run_minutes,
		"performance": performance,
		"good_qty": good_qty,
		"scrap_qty": scrap_qty,
		"quality": quality,
		"oee": oee,
	}


def _unplanned_downtime_minutes(workstation, day_start, day_end):
	DowntimeLog = frappe.qb.DocType("Downtime Log")
	DowntimeReason = frappe.qb.DocType("Downtime Reason")
	total = (
		frappe.qb.from_(DowntimeLog)
		.join(DowntimeReason)
		.on(DowntimeReason.name == DowntimeLog.downtime_reason)
		.select(Sum(DowntimeLog.duration_minutes))
		.where(DowntimeLog.workstation == workstation)
		.where(DowntimeReason.category == "Unplanned")
		.where(DowntimeLog.from_time.between(day_start, day_end))
		.run()
	)[0][0]
	return flt(total)


def _time_logs_for_day(workstation, day_start, day_end):
	JobCardTimeLog = frappe.qb.DocType("Job Card Time Log")
	JobCard = frappe.qb.DocType("Job Card")
	return (
		frappe.qb.from_(JobCardTimeLog)
		.join(JobCard)
		.on(JobCard.name == JobCardTimeLog.parent)
		.select(JobCardTimeLog.completed_qty, JobCardTimeLog.parent.as_("job_card"))
		.where(JobCard.workstation == workstation)
		.where(JobCardTimeLog.from_time.between(day_start, day_end))
		.run(as_dict=True)
	)


def ideal_cycle_time_per_unit(job_card):
	"""Minutes to produce one unit at the Work Order Operation's planned rate.

	Public: also used by floorgraph.scheduling.engine to derive a Job Card's
	planned duration, so that "scheduled at ideal rate" and "Performance = 100%"
	stay the same number instead of two independently-invented ones.
	"""
	work_order, operation = frappe.db.get_value("Job Card", job_card, ["work_order", "operation"])
	if not work_order:
		return 0.0

	time_in_mins = frappe.db.get_value(
		"Work Order Operation", {"parent": work_order, "operation": operation}, "time_in_mins"
	)
	wo_qty = frappe.db.get_value("Work Order", work_order, "qty")
	if not time_in_mins or not wo_qty:
		return 0.0

	return flt(time_in_mins) / flt(wo_qty)


def _last_time_log_date(job_card):
	# Scoped to completed_qty > 0 so this always agrees with job_cards_seen
	# above (which only includes a job card on a day it has a completed_qty>0
	# row). Without this scope, a job card whose globally-last time log was a
	# zero-qty idle/travel entry had its scrap/loss attributed to that idle
	# day - a day it's never in job_cards_seen for - so the scrap/loss was
	# silently dropped on every day, not just misattributed.
	last = frappe.db.get_value(
		"Job Card Time Log",
		{"parent": job_card, "completed_qty": [">", 0]},
		"to_time",
		order_by="to_time desc",
	)
	return getdate(last) if last else None


def _job_card_process_loss_qty(job_card):
	return flt(frappe.db.get_value("Job Card", job_card, "process_loss_qty"))


def upsert_oee_log(workstation, log_date):
	data = compute_workstation_oee(workstation, log_date)
	existing = frappe.db.get_value(
		"Workstation OEE Log", {"workstation": workstation, "log_date": data["log_date"]}, "name"
	)

	if existing:
		doc = frappe.get_doc("Workstation OEE Log", existing)
		doc.update(data)
		doc.save(ignore_permissions=True)
	else:
		doc = frappe.get_doc({"doctype": "Workstation OEE Log", **data})
		doc.insert(ignore_permissions=True)

	return doc.name


def run_daily_oee_job():
	"""Scheduled entry point (see hooks.py scheduler_events) - computes OEE
	for yesterday, once that day's data is finalized. Not used directly by
	tests: they call upsert_oee_log/compute_workstation_oee for a specific
	date instead.

	Company resolution (floorgraph.utils.resolve_company_or_throw, called
	from Workstation OEE Log.before_insert) can fail for a Workstation with
	no Warehouse configured - a per-workstation config gap, not a reason to
	abandon every other workstation's OEE for the day. Skip and log instead
	of letting one bad workstation kill the whole scheduled job."""
	log_date = add_days(today(), -1)
	for workstation in frappe.get_all("Workstation", pluck="name"):
		try:
			upsert_oee_log(workstation, log_date)
		except frappe.ValidationError:
			frappe.log_error(
				title="run_daily_oee_job: could not resolve Company",
				message=frappe.get_traceback(),
			)

	frappe.db.commit()  # nosemgrep: frappe-manual-commit - scheduled job (hooks.py scheduler_events), not a request/test transaction
