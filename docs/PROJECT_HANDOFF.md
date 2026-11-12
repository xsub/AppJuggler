# Keyboard Window Navigator — dokument przekazania projektu

Data: 2026-11-10. Nazwa produktu: **AppJuggler** (*"Jump straight to any app or window from your keyboard."*).
Status: specyfikacja bazowa dla implementacji na macOS (oraz przyszłych adapterów Linux).

## 1. Cel i kontekst

Mała aplikacja działająca w tle pozwala przechodzić do aplikacji i konkretnych okien wyłącznie z klawiatury. Użytkownik pracuje z wieloma aplikacjami, często zajmującymi cały ekran. Chce zastąpić przeglądanie Command+Tab, Docka i Mission Control stałymi adresami klawiaturowymi.

Główna sekwencja: Caps Lock ×2 → litera aplikacji → cyfra okna. Alternatywnie wybór okna strzałkami góra/dół i Enterem. Interfejs wizualny wspomaga naukę; po nauczeniu przypisań nie może spowalniać wpisywania sekwencji.

Nie budujemy menedżera kafelkowania, edytora układów ani zamiennika WindowServera. Pierwszym systemem jest macOS. Ewentualne inne platformy wymagają własnych adapterów systemowych.

## 2. Ustalenia produktu

### 2.1. Wyzwalacz

- Dwa naciśnięcia Caps Lock otwierają tryb nawigacji.
- Domyślne okno czasowe: 300 ms, parametr konfigurowalny i do dostrojenia na sprzęcie użytkownika.
- W normalnym trybie nie blokujemy Caps Lock, nie opóźniamy pojedynczego naciśnięcia i nie odtwarzamy zdarzeń.
- Pierwsze naciśnięcie naturalnie zmienia stan, drugie go przywraca. Diodą zarządza system.
- Użytkownik traktuje podwójne naciśnięcie jako jeden świadomy gest; w tym czasie planuje docelową literę.
- Liczymy dwa odrębne uruchomienia klawisza, a nie bezrefleksyjnie dowolne dwa zdarzenia flagsChanged. Sposób reprezentacji Caps Lock trzeba zweryfikować pomiarem.
- Inne naciśnięcie klawisza pomiędzy naciśnięciami Caps Lock unieważnia kandydaturę gestu. Zwykłe wpisywanie tekstu pozostaje nienaruszone.
- Gdy panel jest otwarty, ponowny pełny gest może go zamykać; to zachowanie robocze do testu ergonomii.

### 2.2. Stałe przypisania aplikacji

- Litery A–Z: warstwa podstawowa. Shift+A–Z: druga warstwa. Łącznie 52 sloty.
- Przypisanie należy do aplikacji, nie do okna ani procesu.
- Kluczem tożsamości aplikacji jest bundle identifier; ścieżka pakietu stanowi wskazówkę uruchomienia, nie podstawową tożsamość.
- Zakończenie aplikacji i restart systemu nie usuwają przypisania.
- Znana aplikacja po ponownym uruchomieniu pojawia się automatycznie pod tą samą literą.
- Zajętego przypisania nie nadpisujemy automatycznie. Zamiana lub usunięcie wymaga jawnej akcji w konfiguracji.
- Aplikacje bez bundle identifier albo wiele instalacji z tym samym identyfikatorem są przypadkami brzegowymi: pokaż konflikt, nie zgaduj.

### 2.3. Mapa klawiatury

Po wyzwoleniu pokazujemy powiększoną mapę klawiatury. Na literach: symbol klawisza, ikona aplikacji i czytelna nazwa. Zamknięte aplikacje są przygaszone, ale przypisania pozostają widoczne.

Przytrzymanie Shift pokazuje drugą warstwę, puszczenie przywraca pierwszą. Litera wybiera aplikację z warstwy aktywnej w chwili keyDown. Shift jest od tego momentu zużyty do wyboru warstwy; jego późniejsze puszczenie nie może zmieniać wybranej aplikacji.

MVP: standardowa geometria ANSI QWERTY z jawnymi etykietami. Obsługa ISO i innych układów jako rozszerzenie. Trzeba jawnie oddzielić fizyczny keycode od litery: domyślny model produktu jest literowy, A–Z niezależnie od Shift. Rozpoznawanie ma ignorować chwilowy stan Caps Lock i nie opierać się na literalnym znaku wielkiej/małej litery. Zachowanie przy zmianie źródła wprowadzania należy przetestować i udokumentować.

### 2.4. Lista okien

Po wybraniu aplikacji mającej wiele okien pokazujemy pionową listę: numer, tytuł i zaznaczenie. Można dołączyć informację o minimalizacji; oznaczenia Space tylko gdy adapter umie je ustalić wiarygodnie.

| Wejście | Zachowanie |
| --- | --- |
| Góra / dół | Zmiana zaznaczenia bez aktywacji okna |
| Enter | Aktywacja zaznaczonego okna i zamknięcie panelu |
| 1–9 | Natychmiastowa aktywacja przypisanego numeru |
| Escape | Zamknięcie całego panelu bez przełączania celu |

Domyślne zaznaczenie: ostatnio używane istniejące okno aplikacji; jeśli brak, pierwsze dostępne. Numery pozostają stałe przez czas życia okien w bieżącej sesji programu. Zmiana fokusu i tytułu nie zmienia numerów.

Po zamknięciu okna 2, okno 3 pozostaje numerem 3. Nowe okno otrzymuje najniższy wolny numer. Pierwsza enumeracja po starcie nadaje numery deterministycznie według dostępnych danych; nie obiecujemy zachowania numeracji po restarcie nawigatora ani aplikacji.

Przy ponad dziewięciu oknach strzałki obsługują całą listę; tylko 1–9 mają skróty bezpośrednie w MVP. Nie wprowadzamy parsowania wielocyfrowego z opóźnieniem.

### 2.5. Jedno okno, brak okien, aplikacja zamknięta

- Jedno kwalifikujące się okno: wybranie litery aktywuje je od razu.
- Zamknięta aplikacja: uruchom ją, obserwuj pojawienie się okien i dopiero wtedy zakończ operację zgodnie z liczbą okien. Nie używaj stałego sleep jako synchronizacji.
- Działająca aplikacja bez okien: aktywuj aplikację. Nie twórz dokumentu lub terminala bez osobno ustalonej reguły.
- Nieistniejący pakiet: pokaż błąd i pozostaw przypisanie do naprawy.

### 2.6. Przypisywanie nowych aplikacji

Obserwuj start i zakończenie aplikacji przez NSWorkspace. Nowa aplikacja bez przypisania trafia do sekcji „Nieprzypisane”; bez wymuszonego okna przerywającego pracę.

Roboczy przepływ konfiguracji: menu paska systemowego → Edytuj przypisania → wybór aplikacji → wybór wolnego klawisza i warstwy → zapis. Ten sam widok mapy może obsługiwać przypisywanie. Nie mieszaj trybu konfiguracji z normalnym wyborem celu.

## 3. Model zdarzeń i szybkość

Stany logiczne: Idle, AppSelection, WindowSelection, LaunchPending, AssignmentEditing, ActivationPending.

- Idle: rozpoznawanie podwójnego Caps Lock; pozostałe klawisze bez zmian.
- AppSelection: Shift zmienia warstwę; litera wybiera aplikację; Escape kończy tryb.
- WindowSelection: cyfry, strzałki i Enter sterują listą; Escape kończy tryb.
- LaunchPending: ograniczona kolejka wejścia dla bieżącej operacji; wyraźna możliwość anulowania.
- ActivationPending: zakończenie po potwierdzeniu fokusu albo kontrolowany błąd.

Stan logiczny musi zmieniać się natychmiast, niezależnie od zakończenia animacji lub renderowania panelu. Szybkie Caps ×2, T, 3 nie może zgubić cyfry, nawet gdy enumeracja okien jeszcze trwa. Kolejka ma limit i token operacji; po anulowaniu lub zmianie celu stare wejście i wyniki asynchroniczne są odrzucane. Jeśli istnieje tylko jedno okno, dodatkowa szybko wpisana cyfra należąca do tej sekwencji nie może wyciec do aplikacji docelowej.

Podczas otwartego panelu przechwytuj wejście i nie przepuszczaj go do poprzedniej aplikacji. Nie odtwarzaj liter ani cyfr po anulowaniu. Dbaj o pary keyDown/keyUp: puszczenie przechwyconego klawisza po zamknięciu panelu także musi zostać obsłużone. Samo obserwowanie globalnych zdarzeń NSEvent nie wystarczy do ich zatrzymywania.

Callback event tap ma wykonywać wyłącznie krótką pracę: dekodowanie, aktualizację lekkiego stanu i przekazanie polecenia. Żadnych blokujących wywołań AX, odczytów dysku ani renderowania. Obsłuż wyłączenie tap przez timeout, sleep/wake i zmianę uprawnień. Na ekranie blokady i przy niedostępności przechwytywania program nie może pozorować działania ani przechowywać wejścia do późniejszego wykonania.

## 4. Integracja z macOS

### 4.1. Wejście

CGEventTap dla keyDown, keyUp i flagsChanged. Do wykrycia gestu wystarcza obserwacja, ale modalny panel wymaga zatrzymywania swoich zdarzeń. Docelowo jeden poprawnie skonfigurowany adapter może wykonywać oba zadania. Dobór miejsca i opcji tap oraz wymaganych zgód sprawdzić na docelowym systemie.

Nie rejestrujemy wpisywanego tekstu. Diagnostyka domyślnie obejmuje przejścia stanów, czasy i błędy; pełne tytuły okien i surowe klawisze nie trafiają do trwałych logów.

### 4.2. Aplikacje i okna

NSWorkspace/NSRunningApplication do cyklu życia i aktywacji aplikacji. Accessibility API do enumeracji i operacji na konkretnych oknach. CGWindowList może dostarczać uzupełniające metadane, ale nie zastępuje uchwytu AX służącego do sterowania.

Nie zakładaj publicznego i niezawodnego mapowania każdego AXUIElement na CGWindowID. Adapter utrzymuje własne opaque WindowHandle i weryfikuje tożsamość w czasie życia procesu. PID nie wystarcza po restarcie procesu. Filtrowanie: zwykłe okna użytkowe; pomijamy pomocnicze powierzchnie, menu i własny panel. Dialogi modalne wymagają osobnej obsługi, nie mechanicznego wyrzucenia z listy.

Aktywacja to operacja z wynikiem: sprawdzenie istnienia, przywrócenie aplikacji/okna jeśli ukryte lub zminimalizowane, aktywacja procesu, podniesienie i fokus odpowiedniego okna, potwierdzenie wyniku. Kolejność i ewentualne ponowienia ustalić pomiarami, nie zakładać że jedno AXRaise zawsze wystarcza. Błędy AX i timeouty mają być ograniczone czasowo i widoczne dla użytkownika.

### 4.3. Spaces i fullscreen

Rozróżniamy zwykłe zmaksymalizowane okno i natywny fullscreen tworzący Space. Pełnoekranowa praca użytkownika jest przypadkiem docelowym; nie zastępuj jej po cichu wymaganiem jednego pulpitu.

Publiczne API pozwala aktywować aplikacje i wykonywać operacje AX, ale nie zapewnia prostego kompletnego interfejsu arbitralnego zarządzania Spaces. Zwykła aktywacja może spowodować przejście do Space przez mechanizmy systemu. Wynik zależy m.in. od ustawień Mission Control, aplikacji i rozmieszczenia okien.

Nie obiecujemy natychmiastowych przejść bez animacji. Nie wprowadzamy prywatnych API, wyłączania SIP ani wstrzykiwania kodu w MVP. Jeśli testy wykażą brak obsługi danego scenariusza, jawnie go dokumentujemy. Rozszerzenia Spaces to osobna decyzja architektoniczna.

### 4.4. Panel

AppKit NSPanel z możliwością pokazania nad aplikacją fullscreen; sprawdzić collectionBehavior i zachowanie na wielu monitorach. Nie zakładać, że panel widoczny na zwykłym pulpicie pojawi się też w fullscreen.

Preferowany panel nieaktywujący: nie odbiera docelowej aplikacji statusu ostatnio aktywnej, a nawigację obsługuje adapter wejścia. Edycja konfiguracji może używać normalnego aktywnego okna. UI na głównym wątku; operacje AX poza callbackiem wejścia, z ograniczonym czasem.

### 4.5. Uprawnienia i pakowanie

Przewidzieć Dostępność oraz ewentualne Monitorowanie wprowadzania zależnie od użytych API. Nie żądać Screen Recording w MVP bez miniatur; każdą dodatkową potrzebę zweryfikować.

Przy skrypcie zgody mogą dotyczyć terminala/interpretera; przy .app docelowego pakietu. Tożsamość podpisu i stabilna lokalizacja wpływają na praktykę ponownego przyznawania zgód. Program ma wykrywać brak uprawnień i pokazywać instrukcję naprawy. Nie obiecywać, że po każdym buildzie zgody zawsze zostaną zachowane.

## 8. Kontrakty komponentów i dane

- `InputAdapter`: zdarzenia klawiszy/modifierów, timestamp monotoniczny, przechwytywanie wejścia panelu.
- `NavigationController`: czysta maszyna stanów; wynikiem są komendy, nie bezpośrednie wywołania systemu.
- `ApplicationRegistry`: zainstalowane/przypisane aplikacje, procesy, start/stop, uruchamianie.
- `WindowRegistry`: odkrywanie, filtrowanie, stabilne sloty w sesji, ostatnio aktywne okno.
- `WindowActivator`: ograniczona czasowo próba aktywacji zwracająca sukces lub opis błędu.
- `OverlayPresenter`: mapa, warstwa Shift, lista i status operacji.
- `BindingStore`: walidacja wersji schematu, konflikty, atomowy zapis.

## 10. Kryteria odbioru

1. Pojedynczy Caps Lock działa normalnie; podwójny otwiera panel i przywraca początkowy stan Caps także wtedy, gdy początkowo był włączony.
2. Zwykłe pisanie i skróty poza panelem pozostają nienaruszone.
3. Caps ×2 → T → 3 działa bez przerw na renderowanie i wybiera oczekiwane okno.
4. Strzałki wyłącznie zaznaczają, Enter aktywuje, Escape nie przełącza celu.
5. Klawisze panelu ani ich puszczenia nie powodują przypadkowego tekstu lub akcji w aplikacji docelowej.
6. Numery nie zmieniają się przy zmianie fokusu/tytułu; zamknięcie okna nie przenumerowuje pozostałych.
7. Restart aplikacji zachowuje literę, a restart nawigatora zachowuje konfigurację obu warstw.
8. Shift wybiera drugą warstwę bez uzależnienia od wcześniejszego stanu Caps Lock.
9. Zamknięcie okna podczas wyświetlania listy nie prowadzi do aktywacji innego okna pod nieaktualnym wyborem. Zmiana generacji listy wymaga odświeżenia lub jawnego rozstrzygnięcia.
10. Brak uprawnień i timeout AX kończą się kontrolowanym komunikatem; callback wejścia nie zawiesza klawiatury.
11. Scenariusze innych Spaces, fullscreen, ukrycia, minimalizacji, wielu monitorów i modalnego dialogu mają zapisane wyniki oraz jawne ograniczenia.
12. Z czystego checkoutu użytkownik może wykonać udokumentowane kroki i uruchomić aplikację.
