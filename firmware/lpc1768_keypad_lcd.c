/* LPC1768 external keypad -> UART0/PuTTY + optional 16x2 LCD.
 * CMSIS: LPC17xx.h, startup_LPC17xx.s, system_LPC17xx.c. One main() only.
 *
 * CNB 1..4 = R1..R4 = P1.23..P1.26 (inputs with pull-ups).
 * CNB 5..8 = C1..C4 = P2.10..P2.13 (scan outputs); JP4 = 1-2.
 * No keypad VCC/GND connection; disconnect onboard CNB3.
 * LCD: CND -> CNAD ribbon, JP5/JP16 fitted, POT3 contrast.
 * UART0: existing PC programming cable at CN9, 9600 baud, 8N1, no flow.
 * FLASH MODE: SW21 both ON; JP3 fitted. Close PuTTY before flashing.
 * RUN MODE: power OFF, SW21 both OFF, JP3 OPEN; power ON, open PuTTY,
 * then RESET with all keys released. Change switches/jumpers with power OFF.
 *
 * Timer0: nominal 1 us free-running counter (wraps at ~71.6 minutes).
 * Timer1: full matrix scan every SCAN_PERIOD_US. Four columns are sampled
 * sequentially, with 5 us settling each. Only one output drives LOW at a time.
 * Debounce: require DEBOUNCE_US stable; timestamp is the first observation
 * of that stable transition, not the later confirmation/transmission time.
 * Resolution is scan-limited, NOT true 1 us key-edge accuracy.
 * Simultaneous/ambiguous keys discard the entry (matrix has no diodes).
 * Foreground UART/LCD output never runs inside the scan ISR.
 * LCD DEMO v1: PC performs authentication and returns a framed result.
 * UART RX interrupt buffers replies during LCD updates. HELLO enables demo.
 * No model runs on the MCU; no host response can grant access after timeout.
 * Original timing capture and collection output remain compatible.
 * 0-9: four-digit entry; *: clear. Other keys are logged, not stored in PIN.
 * After digit 4 is released, report dwell[4], flight[3], total in microseconds.
 * Next digit starts a fresh entry. Entry timeout is 15 seconds.
 */
#include <LPC17xx.h>
#include <stdint.h>
#include <string.h>

#define ENABLE_LCD       1
#define UART_BAUD        9600UL
#define SCAN_PERIOD_US   1000UL
#define DEBOUNCE_US      20000UL
#define ENTRY_TIMEOUT_US 15000000UL
#define ROW_MASK         (0xFUL << 23)
#define COL_MASK         (0xFUL << 10)
#define LCD_DATA         (0xFUL << 23)
#define LCD_RS           (1UL << 27)
#define LCD_EN           (1UL << 28)
#define LCD_MASK         (LCD_DATA | LCD_RS | LCD_EN)
#define QUEUE_SIZE       64UL

static const char keymap[4][4] = {
    {'1','2','3','A'}, {'4','5','6','B'},
    {'7','8','9','C'}, {'*','0','#','D'}
};
typedef struct {
    uint32_t time_us;
    char type; /* D: down, U: up, E: invalid matrix transition */
    char key;
} KeyEvent;
static volatile KeyEvent queue[QUEUE_SIZE];
static volatile uint32_t q_head, q_tail, q_lost;
static uint32_t raw_previous, stable_mask, candidate_since;
static uint32_t blocked, release_since;
static char entry[5] = "____";
static uint32_t downs[4], ups[4], dwell[4], flight[3], total_us;
static uint32_t count, held, completed, lcd_dirty = 1;

#define RX_SIZE 256UL
#define REPLY_TIMEOUT_US 5000000UL
static volatile char rx_buffer[RX_SIZE];
static volatile uint32_t rx_head, rx_tail, rx_fault;
static char rx_line[96], demo_token[17], demo_label[12];
static uint32_t rx_len, rx_discard, demo_mode, pending_reply;
static uint32_t attempt_id, reply_since, ignored_hold;
static char lcd_top[17] = "Enter 4 digits";
static char lcd_bottom[17] = "____";

static void uart_init(void)
{
    uint32_t divisor;
    LPC_SC->PCONP |= 1UL << 3;
    LPC_SC->PCLKSEL0 = (LPC_SC->PCLKSEL0 & ~(3UL << 6)) | (1UL << 6);
    LPC_PINCON->PINSEL0 = (LPC_PINCON->PINSEL0 & ~(0xFUL << 4)) | (5UL << 4);
    LPC_UART0->LCR = 0x83; /* 8 data bits, 1 stop, no parity, DLAB=1 */
    divisor = (SystemCoreClock + 8UL * UART_BAUD) / (16UL * UART_BAUD);
    LPC_UART0->DLM = (divisor >> 8) & 0xFFUL;
    LPC_UART0->DLL = divisor & 0xFFUL;
    LPC_UART0->FDR = 0x10; /* integer divider */
    LPC_UART0->LCR = 3;
    LPC_UART0->IER = 5; /* RX data and line status interrupts */
    NVIC_ClearPendingIRQ(UART0_IRQn);
    NVIC_SetPriority(UART0_IRQn, 3); /* keypad scan has higher priority */
    NVIC_EnableIRQ(UART0_IRQn);
    LPC_UART0->FCR = 7;
    LPC_UART0->TER = 0x80;
}
void UART0_IRQHandler(void)
{
    uint32_t status, next;
    char c;
    for (;;) {
        status = LPC_UART0->LSR;
        if (status & 0x1EUL) rx_fault = 1;
        if (!(status & 1UL)) break;
        c = (char)LPC_UART0->RBR;
        next = (rx_head + 1UL) % RX_SIZE;
        if (next == rx_tail) rx_fault = 1;
        else { rx_buffer[rx_head] = c; __DMB(); rx_head = next; }
    }
}
static void uart_char(char c)
{
#ifdef DEMO_HOST_TEST
    extern void test_tx(char c);
    test_tx(c);
    return;
#endif
    while ((LPC_UART0->LSR & (1UL << 5)) == 0) { }
    LPC_UART0->THR = (uint8_t)c;
}
static void uart_text(const char *s)
{
    while (*s) uart_char(*s++);
}
static void uart_uint(uint32_t n)
{
    char b[10];
    uint32_t i = 0;
    do { b[i++] = (char)('0' + n % 10UL); n /= 10UL; } while (n);
    while (i) uart_char(b[--i]);
}
static void uart_array(const char *label, const uint32_t *v, uint32_t n)
{
    uint32_t i;
    uart_text(label);
    for (i = 0; i < n; ++i) { uart_char(','); uart_uint(v[i]); }
    uart_text("\r\n");
}
static void timer0_init(void)
{
    LPC_SC->PCONP |= 1UL << 1;
    LPC_SC->PCLKSEL0 = (LPC_SC->PCLKSEL0 & ~(3UL << 2)) | (1UL << 2);
    LPC_TIM0->TCR = 2;
    LPC_TIM0->CTCR = 0;
    LPC_TIM0->PR = SystemCoreClock / 1000000UL - 1UL;
    LPC_TIM0->MCR = 0;
    LPC_TIM0->CCR = 0;
    LPC_TIM0->IR = 0xFF;
    LPC_TIM0->TCR = 1;
}
static void delay_us(uint32_t us)
{
#ifdef DEMO_HOST_TEST
    (void)us;
    return;
#endif
    uint32_t start = LPC_TIM0->TC;
    while ((uint32_t)(LPC_TIM0->TC - start) < us) { }
}
static void delay_ms(uint32_t ms)
{
    while (ms--) delay_us(1000);
}
static void lcd_nibble(uint32_t nibble)
{
    LPC_GPIO0->FIOCLR = LCD_DATA;
    LPC_GPIO0->FIOSET = (nibble & 0xFUL) << 23;
    delay_ms(1);
    LPC_GPIO0->FIOSET = LCD_EN;
    delay_ms(1);
    LPC_GPIO0->FIOCLR = LCD_EN;
    delay_ms(1);
}

static void lcd_write(uint8_t value, uint8_t data)
{
    if (data) LPC_GPIO0->FIOSET = LCD_RS;
    else      LPC_GPIO0->FIOCLR = LCD_RS;
    lcd_nibble(value >> 4);
    lcd_nibble(value & 0x0FU);
    delay_ms(2);
}

static void lcd_text(const char *s)
{
    while (*s) lcd_write((uint8_t)*s++, 1);
}

static void lcd_init(void)
{
    /* P0.23..P0.28 -> GPIO. Clear PINSEL1 bits 14..25. */
    LPC_PINCON->PINSEL1 &= ~(0xFFFUL << 14);
    LPC_PINCON->PINMODE_OD0 &= ~LCD_DATA;
    LPC_GPIO0->FIOMASK &= ~LCD_MASK;
    LPC_GPIO0->FIOCLR = LCD_MASK;
    LPC_GPIO0->FIODIR |= LCD_MASK;
    delay_ms(50);

    /* HD44780 power-on sequence, then 4-bit mode. */
    lcd_nibble(3); delay_ms(5);
    lcd_nibble(3); delay_ms(2);
    lcd_nibble(3); delay_ms(2);
    lcd_nibble(2);
    lcd_write(0x28, 0); /* 4-bit, two lines */
    lcd_write(0x08, 0); /* display off */
    lcd_write(0x01, 0); /* clear */
    lcd_write(0x06, 0); /* advance cursor */
    lcd_write(0x0C, 0); /* display on, cursor off */
}


static void keypad_init(void)
{
    LPC_SC->PCONP |= 1UL << 15;
    LPC_PINCON->PINSEL3 &= ~(0xFFUL << 14);
    LPC_PINCON->PINSEL4 &= ~(0xFFUL << 20);
    LPC_PINCON->PINMODE3 &= ~(0xFFUL << 14); /* row pull-ups */
    LPC_PINCON->PINMODE4 &= ~(0xFFUL << 20);
    LPC_PINCON->PINMODE_OD1 &= ~ROW_MASK;
    LPC_PINCON->PINMODE_OD2 &= ~COL_MASK;
    LPC_GPIO1->FIOMASK &= ~ROW_MASK;
    LPC_GPIO2->FIOMASK &= ~COL_MASK;
    LPC_GPIO1->FIODIR &= ~ROW_MASK;
    LPC_GPIO2->FIODIR &= ~COL_MASK;
    LPC_GPIO2->FIOCLR = COL_MASK;
}
static uint32_t matrix_read(void)
{
    uint32_t r, c, rows, mask = 0;
    for (c = 0; c < 4; ++c) {
        LPC_GPIO2->FIODIR &= ~COL_MASK;
        LPC_GPIO2->FIOCLR = COL_MASK;
        LPC_GPIO2->FIODIR |= 1UL << (10 + c);
        delay_us(5);
        rows = ((~LPC_GPIO1->FIOPIN) >> 23) & 0xFUL;
        for (r = 0; r < 4; ++r)
            if (rows & (1UL << r)) mask |= 1UL << (r * 4 + c);
    }
    LPC_GPIO2->FIODIR &= ~COL_MASK;
    return mask;
}
static char mask_key(uint32_t mask)
{
    uint32_t i;
    for (i = 0; i < 16; ++i)
        if (mask & (1UL << i)) return keymap[i / 4][i % 4];
    return '?';
}
static void push_event(char type, char key, uint32_t time_us)
{
    uint32_t next = (q_head + 1UL) % QUEUE_SIZE;
    if (next == q_tail) { ++q_lost; return; }
    queue[q_head].type = type;
    queue[q_head].key = key;
    queue[q_head].time_us = time_us;
    __DMB();
    q_head = next;
}
/* Extracted from ISR so debounce can also be checked with synthetic samples. */
static void debounce_sample(uint32_t raw, uint32_t now)
{
    if (blocked) {
        if (raw != 0) release_since = now;
        else if ((uint32_t)(now - release_since) >= DEBOUNCE_US) {
            blocked = 0;
            stable_mask = raw_previous = 0;
            candidate_since = now;
        }
        return;
    }
    if (raw != 0 && (raw & (raw - 1UL)) != 0) {
        push_event('E', '?', now);
        blocked = 1;
        release_since = now;
        return;
    }
    if (raw != raw_previous) {
        raw_previous = raw;
        candidate_since = now;
    }
    if (raw == stable_mask ||
        (uint32_t)(now - candidate_since) < DEBOUNCE_US) return;
    if (stable_mask && raw) {
        /* Different key without a confirmed release: timing is ambiguous. */
        push_event('E', '?', candidate_since);
        blocked = 1;
        release_since = now;
        return;
    }
    if (stable_mask) push_event('U', mask_key(stable_mask), candidate_since);
    if (raw) push_event('D', mask_key(raw), candidate_since);
    stable_mask = raw;
}
void TIMER1_IRQHandler(void)
{
    uint32_t raw, now;
    if (LPC_TIM1->IR & 1UL) {
        LPC_TIM1->IR = 1;
        raw = matrix_read();
        now = LPC_TIM0->TC;
        debounce_sample(raw, now);
    }
}
static void scan_timer_init(void)
{
    LPC_SC->PCONP |= 1UL << 2;
    LPC_SC->PCLKSEL0 = (LPC_SC->PCLKSEL0 & ~(3UL << 4)) | (1UL << 4);
    LPC_TIM1->TCR = 2;
    LPC_TIM1->CTCR = 0;
    LPC_TIM1->PR = SystemCoreClock / 1000000UL - 1UL;
    LPC_TIM1->MR0 = SCAN_PERIOD_US;
    LPC_TIM1->MCR = 3; /* match0 interrupt and reset */
    LPC_TIM1->CCR = 0;
    LPC_TIM1->IR = 0xFF;
    NVIC_ClearPendingIRQ(TIMER1_IRQn);
    NVIC_SetPriority(TIMER1_IRQn, 2);
    NVIC_EnableIRQ(TIMER1_IRQn);
    LPC_TIM1->TCR = 1;
}
static int pop_event(KeyEvent *e)
{
    uint32_t irq = __get_PRIMASK();
    __disable_irq();
    if (q_head == q_tail) { __set_PRIMASK(irq); return 0; }
    e->type = queue[q_tail].type;
    e->key = queue[q_tail].key;
    e->time_us = queue[q_tail].time_us;
    q_tail = (q_tail + 1UL) % QUEUE_SIZE;
    __set_PRIMASK(irq);
    return 1;
}
static void screen(const char *top, const char *bottom)
{
    strncpy(lcd_top, top, 16); lcd_top[16] = '\0';
    strncpy(lcd_bottom, bottom, 16); lcd_bottom[16] = '\0';
    lcd_dirty = 1;
}
static void prompt(void)
{
    screen(demo_mode ? demo_label : "Enter 4 digits", entry);
}
static void entry_clear(void)
{
    uint32_t i;
    count = held = completed = pending_reply = 0;
    for (i = 0; i < 4; ++i) entry[i] = '_';
    entry[4] = '\0';
    prompt();
}
/* Text protocol is for a supervised demo, not a secure access-control link. */
static int parse_id(const char *s, uint32_t *value)
{
    uint32_t v = 0, d;
    if (!*s) return 0;
    while (*s) {
        if (*s < '0' || *s > '9') return 0;
        d = (uint32_t)(*s++ - '0');
        if (v > (0xFFFFFFFFUL - d) / 10UL) return 0;
        v = v * 10UL + d;
    }
    *value = v; return 1;
}
static void result_timeout(void)
{
    if (pending_reply && (uint32_t)(LPC_TIM0->TC - reply_since) >= REPLY_TIMEOUT_US) {
        pending_reply = 0;
        screen("No PC reply", "Press * to retry");
        uart_text("ERROR,PC_REPLY_TIMEOUT\r\n");
    }
}
static void host_command(char *line)
{
    char *a, *b, *c;
    uint32_t i, id;
    result_timeout();
    if (strncmp(line, "HELLO,", 6) == 0) {
        a = line + 6; b = strchr(a, ',');
        if (!b) return;
        *b++ = '\0';
        if (strlen(a) != 16 || !*b || strlen(b) > 11) return;
        for (i = 0; i < 16; ++i)
            if (!((a[i] >= '0' && a[i] <= '9') || (a[i] >= 'a' && a[i] <= 'f'))) return;
        for (i = 0; b[i]; ++i)
            if (!((b[i] >= 'A' && b[i] <= 'Z') || (b[i] >= 'a' && b[i] <= 'z') ||
                  (b[i] >= '0' && b[i] <= '9') || b[i] == ' ' || b[i] == '_' || b[i] == '-')) return;
        strcpy(demo_token, a); strcpy(demo_label, b);
        demo_mode = 1; attempt_id = 0; ignored_hold = 0;
        entry_clear();
        uart_text("READY,"); uart_text(demo_token); uart_text("\r\n");
        return;
    }
    if (strncmp(line, "RESULT,", 7) != 0) return;
    a = line + 7; b = strchr(a, ','); if (!b) return; *b++ = '\0';
    c = strchr(b, ','); if (!c) return; *c++ = '\0';
    if (!demo_mode || !pending_reply || strcmp(a, demo_token) ||
        !parse_id(b, &id) || id != attempt_id) return;
    if (strcmp(c, "ACCEPT") == 0) screen("Welcome", demo_label);
    else if (strcmp(c, "REJECT") == 0) screen("Access denied", "Press * to retry");
    else if (strcmp(c, "INVALID") == 0) screen("Invalid entry", "Press * to retry");
    else return;
    pending_reply = 0;
    uart_text("ACK,"); uart_text(demo_token); uart_char(','); uart_uint(id);
    uart_char(','); uart_text(c); uart_text("\r\n");
}
static void host_byte(char c)
{
    if (c == '\r') return;
    if (c == '\n') {
        if (!rx_discard) { rx_line[rx_len] = '\0'; host_command(rx_line); }
        rx_len = rx_discard = 0;
    } else if ((unsigned char)c < 32 || (unsigned char)c > 126 || rx_len >= sizeof(rx_line)-1) {
        rx_discard = 1;
    } else if (!rx_discard) rx_line[rx_len++] = c;
}
static void host_poll(void)
{
    uint32_t irq;
    char c;
    if (rx_fault) {
        irq = __get_PRIMASK(); __disable_irq();
        rx_fault = 0; rx_tail = rx_head;
        __set_PRIMASK(irq);
        rx_len = 0; rx_discard = 1;
        pending_reply = 0;
        screen("Serial RX error", "Press * to retry");
        uart_text("ERROR,UART_RX\r\n");
    }
    while (rx_tail != rx_head) {
        c = rx_buffer[rx_tail]; rx_tail = (rx_tail + 1UL) % RX_SIZE;
        host_byte(c);
    }
    result_timeout();
}
static void process_event(const KeyEvent *e)
{
    if (e->type == 'E') {
        ignored_hold = 0;
        entry_clear();
        if (demo_mode) screen("Invalid entry", "Press * to retry");
        uart_text("ERROR,MULTI_OR_AMBIGUOUS_KEYS,ENTRY_DISCARDED\r\n");
        return;
    }
    if (demo_mode && pending_reply && e->key != '*') {
        ignored_hold = e->type == 'D';
        return; /* do not begin another entry while awaiting a decision */
    }
    if (ignored_hold) { if (e->type == 'U') ignored_hold = 0; return; }
    uart_text(e->type == 'D' ? "DOWN," : "UP,");
    uart_char(e->key); uart_char(','); uart_uint(e->time_us); uart_text("\r\n");
    if (e->type == 'D' && e->key == '*') {
        entry_clear(); uart_text("CLEAR\r\n"); return;
    }
    if (e->key < '0' || e->key > '9') return;
    if (e->type == 'D') {
        if (completed) entry_clear();
        if (held || count >= 4) {
            entry_clear(); uart_text("ERROR,ENTRY_SEQUENCE\r\n"); return;
        }
        entry[count] = e->key;
        downs[count] = e->time_us;
        if (count) flight[count - 1] = (uint32_t)(downs[count] - ups[count - 1]);
        held = 1;
        prompt();
        uart_text("ENTRY,"); uart_text(entry); uart_text("\r\n");
    } else if (e->type == 'U' && held && count < 4 && entry[count] == e->key) {
        ups[count] = e->time_us;
        dwell[count] = (uint32_t)(ups[count] - downs[count]);
        held = 0;
        ++count;
        if (count == 4) {
            total_us = (uint32_t)(ups[3] - downs[0]);
            completed = 1;
            if (demo_mode) {
                ++attempt_id; pending_reply = 1;
                screen("Checking...", demo_label);
                uart_text("ATTEMPT,"); uart_text(demo_token); uart_char(',');
                uart_uint(attempt_id); uart_text("\r\n");
            }
            uart_text("PIN,"); uart_text(entry); uart_text("\r\n");
            uart_array("DWELL_US", dwell, 4);
            uart_array("FLIGHT_US", flight, 3);
            uart_text("TOTAL_US,"); uart_uint(total_us); uart_text("\r\n");
            reply_since = LPC_TIM0->TC;
        }
    }
}
int main(void)
{
    KeyEvent e;
    uint32_t irq, lost;
    SystemCoreClockUpdate(); /* normal CMSIS startup calls SystemInit */
    uart_init();
    uart_text("\r\nKEYPAD UART READY - 9600 8N1\r\n");
    if (SystemCoreClock < 1000000UL || SystemCoreClock % 1000000UL != 0) {
        uart_text("ERROR,CORE_CLOCK_NOT_INTEGER_MHZ\r\n");
        while (1) { }
    }
    timer0_init();
    keypad_init();
#if ENABLE_LCD
    lcd_init();
    lcd_text("Enter 4 digits");
#endif
    uart_text("Timing only; * clears; one key at a time.\r\n");
    uart_text("SCAN_US,"); uart_uint(SCAN_PERIOD_US);
    uart_text(",DEBOUNCE_US,"); uart_uint(DEBOUNCE_US); uart_text("\r\n");
    uart_text("Event timestamps are microseconds since reset, scan-limited.\r\n");
    scan_timer_init();
    uart_text("LCD_DEMO,1\r\n");
    while (1) {
        host_poll();
        if (q_lost) {
            irq = __get_PRIMASK(); __disable_irq();
            lost = q_lost; q_lost = 0; q_tail = q_head;
            blocked = 1; release_since = LPC_TIM0->TC;
            __set_PRIMASK(irq);
            entry_clear();
            uart_text("ERROR,QUEUE_OVERFLOW,ENTRY_DISCARDED,");
            uart_uint(lost); uart_text("\r\n");
        }
        if (pop_event(&e)) {
            /* Check entry age using event time BEFORE accepting new data. */
            if (!completed && (held || count) &&
                (uint32_t)(e.time_us - downs[0]) >= ENTRY_TIMEOUT_US) {
                entry_clear(); uart_text("ERROR,ENTRY_TIMEOUT\r\n");
            }
            process_event(&e);
        } else if (!completed && (held || count) &&
                   (uint32_t)(LPC_TIM0->TC - downs[0]) >= ENTRY_TIMEOUT_US) {
            entry_clear(); uart_text("ERROR,ENTRY_TIMEOUT\r\n");
        }
#if ENABLE_LCD
        if (lcd_dirty) {
            uint32_t j;
            lcd_write(0x80, 0); lcd_text(lcd_top);
            for (j = (uint32_t)strlen(lcd_top); j < 16; ++j) lcd_write(' ', 1);
            lcd_write(0xC0, 0); lcd_text(lcd_bottom);
            for (j = (uint32_t)strlen(lcd_bottom); j < 16; ++j) lcd_write(' ', 1);
            lcd_dirty = 0;
        }
#endif
    }
}
