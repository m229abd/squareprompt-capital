import * as React from 'react';
import * as DialogPrimitive from '@radix-ui/react-dialog';
import * as TabsPrimitive from '@radix-ui/react-tabs';
import { cva, type VariantProps } from 'class-variance-authority';
import { clsx, type ClassValue } from 'clsx';
import { twMerge } from 'tailwind-merge';
import { X } from 'lucide-react';
export function cn(...inputs:ClassValue[]){return twMerge(clsx(inputs));}
const buttonVariants=cva('inline-flex items-center justify-center gap-2 whitespace-nowrap rounded-md text-sm font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#95d9e6] disabled:pointer-events-none disabled:opacity-40', {variants:{variant:{default:'bg-[#95d9e6] text-[#151d1f] hover:bg-[#b5e6f0]',outline:'border border-[#2d3536] bg-transparent hover:bg-[#1d2526] text-[#ccd3d4]',ghost:'hover:bg-[#1d2526] text-[#a1acae]',destructive:'bg-red-500/15 text-red-300 hover:bg-red-500/25'},size:{default:'h-10 px-4 py-2',sm:'h-8 px-3 text-xs',icon:'h-9 w-9'}},defaultVariants:{variant:'default',size:'default'}});
export interface ButtonProps extends React.ButtonHTMLAttributes<HTMLButtonElement>,VariantProps<typeof buttonVariants>{}
export const Button=React.forwardRef<HTMLButtonElement,ButtonProps>(({className,variant,size,...props},ref)=><button className={cn(buttonVariants({variant,size,className}))} ref={ref} {...props}/>);
Button.displayName='Button';
export const Input=React.forwardRef<HTMLInputElement,React.InputHTMLAttributes<HTMLInputElement>>(({className,...props},ref)=><input ref={ref} className={cn('flex h-10 w-full rounded-md border border-[#2d3536] bg-[#111617] px-3 py-2 text-sm text-[#e5e9ea] placeholder:text-[#647173] focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-[#95d9e6]',className)} {...props}/>);
Input.displayName='Input';
export const Textarea=React.forwardRef<HTMLTextAreaElement,React.TextareaHTMLAttributes<HTMLTextAreaElement>>(({className,...props},ref)=><textarea ref={ref} className={cn('w-full rounded-md border border-[#2d3536] bg-[#111617] p-3 text-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-[#95d9e6] min-h-28',className)} {...props}/>);
Textarea.displayName='Textarea';
export const Dialog=DialogPrimitive.Root;
export const DialogTrigger=DialogPrimitive.Trigger;
export function DialogContent({children,className,...props}:React.ComponentPropsWithoutRef<typeof DialogPrimitive.Content>){return <DialogPrimitive.Portal><DialogPrimitive.Overlay className="fixed inset-0 z-40 bg-black/70 backdrop-blur-sm"/><DialogPrimitive.Content className={cn('fixed left-1/2 top-1/2 z-50 max-h-[90vh] w-[94vw] max-w-3xl -translate-x-1/2 -translate-y-1/2 overflow-y-auto rounded-xl border border-[#353e40] bg-[#121819] p-7 shadow-2xl',className)} {...props}>{children}<DialogPrimitive.Close className="absolute right-5 top-5 rounded text-[#919c9e] hover:text-white" aria-label="Close"><X size={18}/></DialogPrimitive.Close></DialogPrimitive.Content></DialogPrimitive.Portal>}
export const DialogTitle=DialogPrimitive.Title;
export const DialogDescription=DialogPrimitive.Description;
export const Tabs=TabsPrimitive.Root;
export const TabsContent=TabsPrimitive.Content;
export const TabsList=React.forwardRef<React.ElementRef<typeof TabsPrimitive.List>,React.ComponentPropsWithoutRef<typeof TabsPrimitive.List>>(({className,...props},ref)=><TabsPrimitive.List ref={ref} className={cn('flex gap-6 border-b border-[#252d2f]',className)} {...props}/>);
export const TabsTrigger=React.forwardRef<React.ElementRef<typeof TabsPrimitive.Trigger>,React.ComponentPropsWithoutRef<typeof TabsPrimitive.Trigger>>(({className,...props},ref)=><TabsPrimitive.Trigger ref={ref} className={cn('border-b-2 border-transparent py-4 text-sm text-[#879496] transition data-[state=active]:border-[#95d9e6] data-[state=active]:text-[#d4e9ed]',className)} {...props}/>);
