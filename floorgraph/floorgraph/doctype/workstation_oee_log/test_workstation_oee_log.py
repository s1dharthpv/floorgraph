# Copyright (c) 2026, Sidharth P V and Contributors
# See license.txt

import frappe
from frappe.utils import add_days, get_datetime, getdate

from floorgraph.oee.engine import compute_workstation_oee, upsert_oee_log
from floorgraph.tests import FloorgraphTestCase
from floorgraph.tests.fixtures import TEST_COMPANY
from floorgraph.tests.fixtures import ensure_setup_wizard_fixtures as _ensure_setup_wizard_fixtures
from floorgraph.tests.fixtures import ensure_test_company as _ensure_test_company

# See floorgraph/tests/__init__.py: on Frappe v16+, avoids walking into
# ERPNext's Workstation/Company doctypes, which trips a real ERPNext v16
# bug on a bare site. No-op on v15.
IGNORE_TEST_RECORD_DEPENDENCIES = ["Workstation", "Company"]


def _make_production_scenario(wo_qty, completed_qty, working_hours_end="16:00:00"):
	"""Item + Operation + Workstation + BOM + Work Order + Job Card, all
	freshly created so this test doesn't depend on demo_manufacturing.py's
	fixture data. Workstation working hours are always 08:00-<working_hours_end>.
	Returns (workstation_name, job_card_name, ideal_cycle_time).
	"""
	_ensure_setup_wizard_fixtures()
	_ensure_test_company()
	suffix = frappe.generate_hash(length=8)

	item = frappe.get_doc(
		{
			"doctype": "Item",
			"item_code": f"Test OEE Item {suffix}",
			"item_name": f"Test OEE Item {suffix}",
			"item_group": "Products",
			"stock_uom": "Nos",
			"is_stock_item": 1,
		}
	).insert(ignore_permissions=True)

	raw_material = frappe.get_doc(
		{
			"doctype": "Item",
			"item_code": f"Test OEE Raw Material {suffix}",
			"item_name": f"Test OEE Raw Material {suffix}",
			"item_group": "Raw Material",
			"stock_uom": "Nos",
			"is_stock_item": 1,
		}
	).insert(ignore_permissions=True)

	operation_name = f"Test OEE Operation {suffix}"
	frappe.get_doc({"doctype": "Operation", "name": operation_name}).insert(ignore_permissions=True)

	workstation = frappe.get_doc(
		{
			"doctype": "Workstation",
			"workstation_name": f"Test OEE Workstation {suffix}",
			"production_capacity": 1,
			"working_hours": [{"start_time": "08:00:00", "end_time": working_hours_end, "enabled": 1}],
			# Company resolves via Workstation.warehouse.company (Workstation
			# itself has no company field) - see floorgraph.utils.resolve_company.
			"warehouse": "Work In Progress - _TC",
		}
	).insert(ignore_permissions=True)

	ideal_cycle_time = 10  # minutes per unit, at BOM quantity=1

	bom = frappe.get_doc(
		{
			"doctype": "BOM",
			"item": item.name,
			"quantity": 1,
			"company": TEST_COMPANY,
			"with_operations": 1,
			"items": [{"item_code": raw_material.name, "qty": 1, "uom": "Nos", "rate": 1}],
			"operations": [
				{
					"operation": operation_name,
					"workstation": workstation.name,
					"time_in_mins": ideal_cycle_time,
				}
			],
		}
	)
	bom.insert(ignore_permissions=True)
	bom.submit()

	work_order = frappe.get_doc(
		{
			"doctype": "Work Order",
			"production_item": item.name,
			"bom_no": bom.name,
			"qty": wo_qty,
			"company": TEST_COMPANY,
			"fg_warehouse": "Finished Goods - _TC",
			"wip_warehouse": "Work In Progress - _TC",
			"planned_start_date": frappe.utils.now_datetime(),
			"operations": [
				{
					"operation": operation_name,
					"workstation": workstation.name,
					"time_in_mins": ideal_cycle_time * wo_qty,
				}
			],
		}
	)
	work_order.insert(ignore_permissions=True)
	work_order.submit()

	job_card = frappe.get_doc("Job Card", {"work_order": work_order.name})

	log_date = getdate()
	job_card.append(
		"time_logs",
		{
			"from_time": get_datetime(f"{log_date} 09:00:00"),
			"to_time": get_datetime(f"{log_date} 10:00:00"),
			"completed_qty": completed_qty,
		},
	)
	job_card.save(ignore_permissions=True)

	return workstation.name, job_card.name, ideal_cycle_time


class TestWorkstationOEELog(FloorgraphTestCase):
	def test_availability_reflects_unplanned_downtime(self):
		workstation, _job_card, _ideal = _make_production_scenario(wo_qty=1, completed_qty=0)
		log_date = getdate()

		frappe.get_doc(
			{
				"doctype": "Downtime Log",
				"workstation": workstation,
				"downtime_reason": "Breakdown",
				"from_time": get_datetime(f"{log_date} 09:00:00"),
				"to_time": get_datetime(f"{log_date} 09:30:00"),
			}
		).insert(ignore_permissions=True)

		data = compute_workstation_oee(workstation, log_date)

		# Hand check: 480 planned minutes (08:00-16:00), 30 unplanned downtime
		# -> run = 450, availability = 450/480 = 93.75%. Neither 0 nor 100 -
		# proves the denominator isn't tautologically derived from the same
		# activity it's supposed to measure.
		self.assertEqual(data["planned_minutes"], 480)
		self.assertEqual(data["unplanned_downtime_minutes"], 30)
		self.assertEqual(data["run_minutes"], 450)
		self.assertAlmostEqual(data["availability"], 93.75, places=2)

	def test_availability_is_zero_on_a_holiday_not_100_percent(self):
		workstation, _job_card, _ideal = _make_production_scenario(wo_qty=1, completed_qty=0)
		log_date = getdate()

		holiday_list = frappe.get_doc(
			{
				"doctype": "Holiday List",
				"holiday_list_name": f"Test OEE Holidays {frappe.generate_hash(length=8)}",
				"from_date": log_date,
				"to_date": log_date,
				"holidays": [{"holiday_date": log_date, "description": "Test Holiday"}],
			}
		).insert(ignore_permissions=True)
		frappe.db.set_value("Workstation", workstation, "holiday_list", holiday_list.name)

		data = compute_workstation_oee(workstation, log_date)

		# Before the fix: planned_minutes stayed the full 480 despite the
		# plant being closed, and zero logged downtime (there was nothing to
		# log) reported 100% Availability for a day nothing ran.
		self.assertEqual(data["planned_minutes"], 0)
		self.assertEqual(data["availability"], 0)

	def test_scrap_qty_not_lost_when_job_cards_last_time_log_is_zero_qty(self):
		workstation, job_card, _ideal = _make_production_scenario(wo_qty=20, completed_qty=15)
		log_date = getdate()
		self._append_trailing_idle_time_log(job_card, log_date)

		self.assertEqual(frappe.db.get_value("Job Card", job_card, "process_loss_qty"), 5)

		data = compute_workstation_oee(workstation, log_date)
		self.assertEqual(data["scrap_qty"], 5)
		self.assertEqual(data["good_qty"], 15)

	def _append_trailing_idle_time_log(self, job_card, log_date):
		# A zero-qty idle/travel entry dated AFTER log_date - before the fix,
		# this became the job card's globally-last time log and silently
		# stole the scrap/loss attribution date. job_cards_seen never
		# includes a job card on a day with no completed_qty>0 row, so the
		# scrap/loss vanished on every day, not just this one.
		jc = frappe.get_doc("Job Card", job_card)
		jc.append(
			"time_logs",
			{
				"from_time": get_datetime(f"{add_days(log_date, 1)} 09:00:00"),
				"to_time": get_datetime(f"{add_days(log_date, 1)} 09:30:00"),
				"completed_qty": 0,
			},
		)
		jc.save(ignore_permissions=True)

	def test_performance_and_quality_from_job_card_activity(self):
		# Same definition on ERPNext v15 and v16: quality comes from the Job
		# Card's computed process_loss_qty (units never completed). Work order
		# qty=20, but the time log only completes 15 - the other 5 are a
		# process loss, derived automatically by the Job Card as
		# for_quantity - total_completed_qty (- pending_qty on v16, 0 here).
		# Planned=480, no downtime -> run=480. Ideal run = 10 * 15 = 150 min
		# (only completed units count toward performance).
		# Performance = 150/480 = 31.25%.
		workstation, job_card, ideal_cycle_time = _make_production_scenario(wo_qty=20, completed_qty=15)
		log_date = getdate()

		self.assertEqual(
			frappe.db.get_value("Job Card", job_card, "process_loss_qty"),
			5,
			"Job Card should have derived a 5-unit process loss (20 - 15 - 0 pending).",
		)

		data = compute_workstation_oee(workstation, log_date)

		self.assertEqual(data["actual_qty"], 15)
		self.assertAlmostEqual(data["ideal_run_minutes"], ideal_cycle_time * 15, places=2)
		self.assertAlmostEqual(data["performance"], (ideal_cycle_time * 15) / 480 * 100, places=2)
		self.assertEqual(data["scrap_qty"], 5)
		self.assertEqual(data["good_qty"], 15)
		self.assertAlmostEqual(data["quality"], 75.0, places=2)

	def test_v15_scrap_items_table_does_not_change_quality(self):
		# ERPNext v15 still has Job Card.scrap_items; v16 removed it. Quality
		# is defined the same way on both, from process_loss_qty only, so
		# scrap item rows must not move the number.
		if not frappe.get_meta("Job Card").has_field("scrap_items"):
			self.skipTest("Job Card has no scrap_items table on this ERPNext version")

		workstation, job_card, _ideal = _make_production_scenario(wo_qty=20, completed_qty=20)
		log_date = getdate()
		jc = frappe.get_doc("Job Card", job_card)
		jc.append(
			"scrap_items",
			{"item_code": jc.production_item, "stock_qty": 5, "stock_uom": "Nos"},
		)
		jc.save(ignore_permissions=True)

		data = compute_workstation_oee(workstation, log_date)
		self.assertEqual(data["scrap_qty"], 0)
		self.assertEqual(data["good_qty"], 20)
		self.assertAlmostEqual(data["quality"], 100.0, places=2)

	def test_upsert_creates_then_updates_single_row_per_workstation_per_day(self):
		workstation, _job_card, _ideal = _make_production_scenario(wo_qty=1, completed_qty=0)
		log_date = getdate()

		name_1 = upsert_oee_log(workstation, log_date)
		name_2 = upsert_oee_log(workstation, log_date)

		self.assertEqual(name_1, name_2)
		self.assertEqual(
			frappe.db.count("Workstation OEE Log", {"workstation": workstation, "log_date": log_date}), 1
		)
