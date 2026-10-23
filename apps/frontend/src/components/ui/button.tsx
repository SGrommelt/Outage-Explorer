import type { ComponentProps } from "react";
import { cn } from "@/lib/utils";

export function Button({ className, ...props }: ComponentProps<"button">) {
  return <button className={cn("rounded-md bg-slate-900 px-4 py-2 text-sm font-medium text-white enabled:hover:bg-slate-700 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-600 disabled:cursor-not-allowed disabled:opacity-60", className)} {...props} />;
}
