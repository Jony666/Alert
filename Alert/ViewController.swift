//
//  ViewController.swift
//  Alert
//
//  Created by Jony Wang on 2018/7/30.
//  Copyright © 2018年 Jony. All rights reserved.
//

import UIKit

class ViewController: UIViewController {

    override func viewDidLoad() {
        super.viewDidLoad()
        // Do any additional setup after loading the view, typically from a nib.
    }
    @IBAction func showAlert(_ sender: UIButton) {
        let alert = UIAlertController(title: "Air Drop", message: "Jony wants to share this photo", preferredStyle: .alert)
        
        guard let image = UIImage(named: "myCat") else { return }
        let maxSize = CGSize(width: 245, height: 300)
        let imageSize = image.size
        var ratio : CGFloat
        if imageSize.height > imageSize.width {
            ratio = maxSize.height / imageSize.height
        }else{
            ratio = maxSize.width / imageSize.width
        }
        let scaledSize = CGSize(width: imageSize.width * ratio, height: imageSize.height * ratio)
        let scledImage = resizeImage(image: image, targetSize: scaledSize)
        
        let addImage = UIAlertAction(title: "", style: .default, handler: nil)
        addImage.isEnabled = false
        addImage.setValue(scledImage.withRenderingMode(.alwaysOriginal), forKey: "image")
        alert.addAction(addImage)
        
        let decline = UIAlertAction(title: "Decline", style: .cancel, handler: nil)
        let accept = UIAlertAction(title: "Accept", style: .default, handler: nil)
        alert.addAction(decline)
        alert.addAction(accept)
        present(alert, animated: true, completion: nil)
    }
    
    func resizeImage(image: UIImage, targetSize: CGSize) -> UIImage {
        let size = image.size
        let widthRatio = targetSize.width / size.width
        let heightRatio = targetSize.height / size.height
        
        var newSize: CGSize
        if(widthRatio>heightRatio){
            newSize = CGSize(width: size.width * heightRatio , height: size.height * heightRatio)
        }else{
            newSize = CGSize(width: size.width * widthRatio, height: size.height * widthRatio)
        }
        let rect = CGRect(x: 0, y: 0, width: newSize.width, height: newSize.height)
        UIGraphicsBeginImageContextWithOptions(newSize, false, 1.0)
        image.draw(in: rect)
        let newImage = UIGraphicsGetImageFromCurrentImageContext()
        UIGraphicsEndImageContext()
        return newImage!
    }
    

}

