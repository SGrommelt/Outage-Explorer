import { useMemo } from "react";
import { flexRender, getCoreRowModel, useReactTable, type ColumnDef } from "@tanstack/react-table";
import { formatDecimal } from "@/lib/format";
import type { Cell, Column } from "@/lib/data";

export function DataTable({ columns, rows, caption }: { columns: Column[]; rows: Cell[][]; caption: string }) {
  const definitions = useMemo<ColumnDef<Cell[]>[]>(() => columns.map((column, index) => ({
    id: String(index), accessorFn: row => row[index],
    header: () => <>{column.name}<span className="block text-xs font-normal text-slate-500">{column.type}</span></>,
    cell: context => context.getValue() === null ? <span className="text-slate-500">NULL</span> : (/(_mw$|_pct$|percentage$)/i.test(column.name) || column.type === "MW" || column.type.startsWith("%")) ? formatDecimal(context.getValue() as Cell) : String(context.getValue() ?? ""),
  })), [columns]);
  const table = useReactTable({ data: rows, columns: definitions, getCoreRowModel: getCoreRowModel(), manualPagination: true });
  return <div className="mt-5 overflow-x-auto rounded-md border border-slate-200" tabIndex={0} role="region" aria-label={caption}>
    <table className="w-full text-left text-sm"><caption className="sr-only">{caption}</caption>
      <thead className="bg-slate-50">{table.getHeaderGroups().map(group => <tr key={group.id}>{group.headers.map(header => <th scope="col" className="whitespace-nowrap px-4 py-3" key={header.id}>{flexRender(header.column.columnDef.header, header.getContext())}</th>)}</tr>)}</thead>
      <tbody>{table.getRowModel().rows.map(row => <tr key={row.id} className="border-t border-slate-200">{row.getVisibleCells().map(cell => <td key={cell.id} className="whitespace-nowrap px-4 py-3">{flexRender(cell.column.columnDef.cell, cell.getContext())}</td>)}</tr>)}</tbody>
    </table>
    {rows.length === 0 && <p className="p-4 text-sm text-slate-600">No rows match this request.</p>}
  </div>;
}
