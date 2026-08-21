import * as ToastPrimitive from "@radix-ui/react-toast";
import { X } from "lucide-react";
import { cn } from "../../lib/utils";

export function Toast({ message, tone = "success", onClose }: { message: string; tone?: "success" | "error"; onClose: () => void }) {
  return <ToastPrimitive.Provider duration={4200} swipeDirection="right"><ToastPrimitive.Root role={tone === "error" ? "alert" : "status"} className={cn("ui-toast", tone === "error" && "ui-toast-error")} defaultOpen onOpenChange={(open) => { if (!open) onClose(); }}><ToastPrimitive.Description>{message}</ToastPrimitive.Description><ToastPrimitive.Close className="ui-toast-close" aria-label="关闭通知"><X size={16} /></ToastPrimitive.Close></ToastPrimitive.Root><ToastPrimitive.Viewport className="ui-toast-viewport" /></ToastPrimitive.Provider>;
}
