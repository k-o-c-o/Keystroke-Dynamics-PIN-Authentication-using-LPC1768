/* gcc -std=c99 -Itests tests/test_firmware.c -o /tmp/keypad_fw_test */
#include <assert.h>
#include <stdio.h>
#include <string.h>
#define DEMO_HOST_TEST
#define main firmware_main
#include "../firmware/lpc1768_keypad_lcd.c"
#undef main
static char output[16384];static unsigned length;
void test_tx(char c) { assert(length<sizeof(output)-1);output[length++]=c;output[length]=0; }
static void command(const char *s) { while(*s)host_byte(*s++); }
static void event(char type,char key,uint32_t t) { KeyEvent e;e.type=type;e.key=key;e.time_us=t;LPC_TIM0->TC=t;process_event(&e); }
static void pin_entry(void) {
    uint32_t i;
    for(i=0;i<4;i++) {event('D',(char)('1'+i),1000000+i*250000);event('U',(char)('1'+i),1100000+i*250000);}
}
int main(void) {
    uint32_t n;
    command("HELLO,0123456789abcdef,User 1\n");
    assert(demo_mode && !pending_reply && !strcmp(lcd_top,"User 1"));
    pin_entry();assert(pending_reply && attempt_id==1);
    assert(strstr(output,"ATTEMPT,0123456789abcdef,1\r\n"));
    assert(strstr(output,"DWELL_US,100000,100000,100000,100000"));
    assert(strstr(output,"TOTAL_US,850000"));
    command("RESULT,ffffffffffffffff,1,ACCEPT\n");assert(pending_reply);
    command("RESULT,0123456789abcdef,2,ACCEPT\n");assert(pending_reply);
    command("RESULT,0123456789abcdef,1,ACCEPT\n");
    assert(!pending_reply && !strcmp(lcd_top,"Welcome") && !strcmp(lcd_bottom,"User 1"));
    command("RESULT,0123456789abcdef,1,REJECT\n");assert(!strcmp(lcd_top,"Welcome"));
    event('D','*',2000000);event('U','*',2100000);pin_entry();
    command("RESULT,0123456789abcdef,2,REJECT\n");assert(!strcmp(lcd_top,"Access denied"));
    pin_entry();assert(pending_reply);LPC_TIM0->TC=reply_since+REPLY_TIMEOUT_US;
    command("RESULT,0123456789abcdef,3,ACCEPT\n");assert(!pending_reply && !strcmp(lcd_top,"No PC reply"));
    pin_entry();event('D','*',2500000);command("RESULT,0123456789abcdef,4,ACCEPT\n");
    assert(!pending_reply && strcmp(lcd_top,"Welcome"));
    assert(!parse_id("4294967296",&n) && parse_id("4294967295",&n));
    pin_entry();command("RESULT,0123456789abcdef,5,INVALID\n");assert(!strcmp(lcd_top,"Invalid entry"));
    pin_entry();reply_since=0xFFFFFF00UL;LPC_TIM0->TC=(uint32_t)(reply_since+REPLY_TIMEOUT_US);
    result_timeout();assert(!pending_reply);
    command("HELLO,xxxxxxxxxxxxxxxx,User 2\n");assert(!strcmp(demo_label,"User 1"));
    command("HELLO,abcdef0123456789,User 2\n");assert(!strcmp(demo_label,"User 2") && attempt_id==0);
    pin_entry();for(n=0;n<110;n++)host_byte('X');host_byte('\n');assert(pending_reply);
    command("RESULT,abcdef0123456789,1,ACCEPT\n");assert(!strcmp(lcd_bottom,"User 2"));
    rx_fault=1;host_poll();assert(!strcmp(lcd_top,"Serial RX error"));
    puts("Firmware protocol tests passed (host mock, not physical hardware).");
    return 0;
}
