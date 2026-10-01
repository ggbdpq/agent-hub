import { createBrowserRouter } from "react-router";
import { AppLayout } from "@/layouts/app-layout";
import { ChatPage } from "@/features/chat/chat-page";
import { TasksPage } from "@/features/tasks/tasks-page";
import { OrdersPage } from "@/features/orders/orders-page";
import NotFound from "@/pages/not-found";

export const router = createBrowserRouter([
  {
    element: <AppLayout />,
    children: [
      { path: "/", element: <ChatPage /> },
      { path: "/tasks", element: <TasksPage /> },
      { path: "/orders", element: <OrdersPage /> },
    ],
  },
  { path: "/404", element: <NotFound /> },
  { path: "*", element: <NotFound /> },
]);
