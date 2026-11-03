/* HOST TEST STUB ONLY. Never add this header to the Keil project. */
#include <stdint.h>
typedef struct { volatile uint32_t PCONP,PCLKSEL0; } SC;
typedef struct { volatile uint32_t PINSEL0,PINSEL1,PINSEL3,PINSEL4,PINMODE3,PINMODE4,PINMODE_OD0,PINMODE_OD1,PINMODE_OD2; } PIN;
typedef struct { volatile uint32_t FIOCLR,FIOSET,FIOMASK,FIODIR,FIOPIN; } GPIO;
typedef struct { volatile uint32_t LCR,DLM,DLL,FDR,IER,FCR,TER,LSR,THR,RBR; } UART;
typedef struct { volatile uint32_t TCR,CTCR,PR,MCR,CCR,IR,TC,MR0; } TIMER;
static SC sc;static PIN pin;static GPIO g0,g1,g2;static UART uart;static TIMER t0,t1;
#define LPC_SC (&sc)
#define LPC_PINCON (&pin)
#define LPC_GPIO0 (&g0)
#define LPC_GPIO1 (&g1)
#define LPC_GPIO2 (&g2)
#define LPC_UART0 (&uart)
#define LPC_TIM0 (&t0)
#define LPC_TIM1 (&t1)
static uint32_t SystemCoreClock=100000000;
#define SystemCoreClockUpdate() ((void)0)
#define NVIC_ClearPendingIRQ(x) ((void)0)
#define NVIC_SetPriority(x,y) ((void)0)
#define NVIC_EnableIRQ(x) ((void)0)
#define __DMB() ((void)0)
#define __get_PRIMASK() 0
#define __disable_irq() ((void)0)
#define __set_PRIMASK(x) ((void)(x))
