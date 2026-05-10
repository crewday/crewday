import type { PaySlip } from "@/types/api";

export type PayPageSlip = PaySlip & { pay_period_id: string };

export interface MoneyPayload {
  cents: number;
  currency: string;
}

export interface PayrollPayslipPayload {
  id: string;
  pay_period_id: string;
  user_id: string;
  currency: string;
  shift_hours_decimal: number | string;
  overtime_hours_decimal: number | string;
  gross: MoneyPayload;
  expense_reimbursements: MoneyPayload;
  net: MoneyPayload;
  status: string;
}

export interface PayrollPayslipListPayload {
  data: PayrollPayslipPayload[];
}

export function normalizePayslipStatus(status: string): PaySlip["status"] {
  if (status === "draft" || status === "issued" || status === "paid" || status === "voided") {
    return status;
  }
  return "draft";
}

export function mapPayrollPayslip(p: PayrollPayslipPayload): PayPageSlip {
  return {
    id: p.id,
    pay_period_id: p.pay_period_id,
    employee_id: p.user_id,
    period_starts: "",
    period_ends: "",
    gross_cents: p.gross.cents,
    reimbursements_cents: p.expense_reimbursements.cents,
    net_cents: p.net.cents,
    status: normalizePayslipStatus(p.status),
    hours: Number(p.shift_hours_decimal),
    overtime: Number(p.overtime_hours_decimal),
    currency: p.currency,
    locale: "en",
    jurisdiction: "",
  };
}
