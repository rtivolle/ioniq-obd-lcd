#!/usr/bin/env python3
import sys
import board
import digitalio
import adafruit_character_lcd.character_lcd as characterlcd

def get_lcd():
    lcd_rs = digitalio.DigitalInOut(board.D22)
    lcd_en = digitalio.DigitalInOut(board.D17)
    lcd_d4 = digitalio.DigitalInOut(board.D25)
    lcd_d5 = digitalio.DigitalInOut(board.D24)
    lcd_d6 = digitalio.DigitalInOut(board.D23)
    lcd_d7 = digitalio.DigitalInOut(board.D18)
    return characterlcd.Character_LCD_Mono(lcd_rs, lcd_en, lcd_d4, lcd_d5, lcd_d6, lcd_d7, 16, 2)

def main():
    if len(sys.argv) > 1:
        message = " ".join(sys.argv[1:])
        message = message.replace("\\n", "\n")
    else:
        message = "Hello, World!\nRaspberry Pi 3B"

    lcd = get_lcd()
    lcd.clear()
    lcd.message = message
    print(f"Displayed on LCD:\n{message}")

if __name__ == "__main__":
    main()
