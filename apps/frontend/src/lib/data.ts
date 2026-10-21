import { ApiError } from "./api";

export interface Column { name: string; type: string }
export interface Dataset { name: string; description: string; columns: Column[] }
// Decimal values and dates arrive as strings; preserve them without Number coercion.
export type Cell = string | number | null;
export interface Catalog { datasets: Dataset[] }
export interface Preview { dataset: string; page: number; page_size: number; total: number; rows: Record<string, Cell>[] }
export interface QueryResult { columns: Column[]; rows: Cell[][]; row_count: number; row_limit: number; truncated: boolean }
export const inputStyle = "w-full rounded-md border border-slate-300 px-3 py-2 text-sm focus:outline-blue-600";
export function dataError(error: unknown): string {
  if (!(error instanceof ApiError)) return "Unable to load data. Please try again.";
  switch (error.status) {
    case 400: case 422: return "Invalid filters or unsupported SQL. Check your input and the supported query syntax.";
    case 403: return "You do not have permission to access this dataset or query.";
    case 404: return "The requested dataset is not available.";
    case 503: return "Published data is unavailable. A complete snapshot must be published before exploring data.";
    default: return error.message;
  }
}
