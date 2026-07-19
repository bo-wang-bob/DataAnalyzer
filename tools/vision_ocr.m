#import <AppKit/AppKit.h>
#import <Foundation/Foundation.h>
#import <Vision/Vision.h>

static void Fail(NSString *message) {
    fprintf(stderr, "%s\n", message.UTF8String);
    exit(1);
}

int main(int argc, const char *argv[]) {
    @autoreleasepool {
        if (argc < 2) {
            Fail(@"usage: vision_ocr IMAGE_PATH");
        }
        NSString *path = [NSString stringWithUTF8String:argv[1]];
        NSImage *image = [[NSImage alloc] initWithContentsOfFile:path];
        if (!image) {
            Fail([NSString stringWithFormat:@"cannot open image: %@", path]);
        }
        NSRect proposed = NSMakeRect(0, 0, image.size.width, image.size.height);
        CGImageRef cgImage = [image CGImageForProposedRect:&proposed context:nil hints:nil];
        if (!cgImage) {
            Fail([NSString stringWithFormat:@"cannot create CGImage: %@", path]);
        }

        VNRecognizeTextRequest *request = [[VNRecognizeTextRequest alloc] init];
        request.recognitionLevel = VNRequestTextRecognitionLevelAccurate;
        request.usesLanguageCorrection = YES;
        if (@available(macOS 13.0, *)) {
            request.automaticallyDetectsLanguage = YES;
        }
        VNImageRequestHandler *handler = [[VNImageRequestHandler alloc]
            initWithCGImage:cgImage
            options:@{}];
        NSError *error = nil;
        if (![handler performRequests:@[request] error:&error]) {
            Fail([NSString stringWithFormat:@"Vision OCR failed: %@", error.localizedDescription]);
        }

        NSMutableArray *items = [NSMutableArray array];
        for (VNRecognizedTextObservation *observation in request.results ?: @[]) {
            VNRecognizedText *candidate = [[observation topCandidates:1] firstObject];
            if (!candidate) {
                continue;
            }
            CGRect box = observation.boundingBox;
            [items addObject:@{
                @"text": candidate.string ?: @"",
                @"confidence": @(candidate.confidence),
                @"bbox": @[@(box.origin.x), @(box.origin.y), @(box.size.width), @(box.size.height)]
            }];
        }
        NSData *json = [NSJSONSerialization dataWithJSONObject:items options:0 error:&error];
        if (!json) {
            Fail([NSString stringWithFormat:@"cannot encode OCR output: %@", error.localizedDescription]);
        }
        fwrite(json.bytes, 1, json.length, stdout);
        fputc('\n', stdout);
    }
    return 0;
}
