import { Outlet } from "react-router";
import { Flower2, ListChecks, ReceiptText } from "lucide-react";
import { NavLink } from "react-router";
import { cn } from "@/lib/utils";

const NAV_ITEMS = [
  { to: "/", title: "花语顾问", icon: Flower2, end: true },
  { to: "/tasks", title: "养护方案", icon: ListChecks },
  { to: "/orders", title: "订单支付", icon: ReceiptText },
];

/** 主布局：顶部导航 + 内容出口。响应式优先，跨端由契约层保证（见 README）。 */
export function AppLayout() {
  return (
    <div className="flex h-screen flex-col bg-background text-foreground">
      <header className="flex h-14 shrink-0 items-center gap-6 border-b px-5">
        <div className="flex items-center gap-2 text-base font-semibold">
          <Flower2 className="size-5 text-flora-primary" />
          花语智能体
        </div>
        <nav className="flex items-center gap-1">
          {NAV_ITEMS.map(({ to, title, icon: Icon, end }) => (
            <NavLink
              key={to}
              to={to}
              end={end}
              className={({ isActive }) =>
                cn(
                  "flex items-center gap-1.5 rounded-md px-3 py-1.5 text-sm transition-colors",
                  isActive
                    ? "bg-accent font-medium text-accent-foreground"
                    : "text-muted-foreground hover:bg-accent/60 hover:text-foreground",
                )
              }
            >
              <Icon className="size-4" />
              {title}
            </NavLink>
          ))}
        </nav>
      </header>
      <main className="min-h-0 flex-1 overflow-hidden">
        <Outlet />
      </main>
    </div>
  );
}
